# 第2章：开源源码对照与完整复核

日期：2026-11-06。原书《System Design Interview.md》26–523行，讨论单机、Web/数据库分离、缓存/CDN、无状态服务、复制、数据中心、消息队列、监控及分片。项目此前已有2026-10-27/10-29审查；本轮补充实际GitHub源码对照，并重新检查可复现的并发边界，不能把缺少本文件误说成整章从未实现。

## GitHub检索和固定版本

通过GitHub连接器查询`repo:haproxy/haproxy`与`repo:redis/redis`，核实主仓库后读取固定源码。HAProxy使用查询时master提交，Redis固定8.0.5标签指向的提交。这里只比较源码，没有安装、运行或复制上游，也不增加项目依赖。

| 项目 | 固定提交 | 实际选读 |
|---|---|---|
| [HAProxy](https://github.com/haproxy/haproxy/tree/64d597032b68c2f43ac1e5b7cb18b8923caf0f89) | `64d597032b68c2f43ac1e5b7cb18b8923caf0f89` | `src/lb_fwrr.c`的上下线/加权轮询，`src/backend.c`连接重试预算 |
| [Redis 8.0.5](https://github.com/redis/redis/tree/214a4905c3edb6b69ac9a9c5d769b7d3fb81ed54) | `214a4905c3edb6b69ac9a9c5d769b7d3fb81ed54` | `src/evict.c`内存预算与采样淘汰，`src/expire.c`主动过期的时间预算 |

## HAProxy：调度状态、故障和重试各有边界

- [`fwrr_set_server_status_down`](https://github.com/haproxy/haproxy/blob/64d597032b68c2f43ac1e5b7cb18b8923caf0f89/src/lb_fwrr.c#L37-L88)在写锁内把不可用服务器移出对应队列/树并更新权重；[`fwrr_get_next_server`](https://github.com/haproxy/haproxy/blob/64d597032b68c2f43ac1e5b7cb18b8923caf0f89/src/lb_fwrr.c#L556-L669)选择活动或备用组，跳过已饱和/需避开的服务器，并在返回前释放调度锁。
- 这与本地[LoadBalancer.forward](../src/scale_from_zero/balancer.py)“锁内获取轮询起点及端点快照，锁外RPC”有可比的状态归属思路，但本地是等权轮询、请求时失败回退，无后台健康检查、权重树或自动发现，不能称为HAProxy复刻。
- [`backend.c`连接失败路径](https://github.com/haproxy/haproxy/blob/64d597032b68c2f43ac1e5b7cb18b8923caf0f89/src/backend.c#L2810-L2890)检查连接重试次数/策略，释放旧端点；证书SNI不匹配会阻止连接重试。它并不证明业务POST可任意重放。本地仅GET及带稳定请求身份的资料更新可以自动尝试下一Web，登录不自动重试。

## Redis：容量淘汰不等于复制新鲜度

- [`performEvictions`](https://github.com/redis/redis/blob/214a4905c3edb6b69ac9a9c5d769b7d3fb81ed54/src/evict.c#L480-L610)按内存预算处理淘汰，受tenacity计算的运行时间约束；LRU/LFU/TTL分支通过候选池和采样挑选key。本地[Cache](../src/scale_from_zero/cache.py)为有界小缓存，LRU/FIFO使用OrderedDict，LFU直接遍历计数，不采用相同采样算法。
- [`activeExpireCycle`](https://github.com/redis/redis/blob/214a4905c3edb6b69ac9a9c5d769b7d3fb81ed54/src/expire.c#L188-L220)有配置化工作预算和跨调用状态。读取过期与后台清理并不是“到TTL时所有内存立即释放”。本地在读取/写入时处理过期，属于不同实现。
- TTL只约束缓存项寿命，不保证复制源已经追到某个版本。本地的generation与版本下限阻止已观察失效后的旧回填；失败失效、缓存重启或元数据淘汰仍会影响新鲜度，强读/min_version另有明确契约。

## CodeGraph核查及确认修复

CodeGraph读取了[资料事务](../src/scale_from_zero/profiles.py)、[共享Fence](../src/scale_from_zero/leadership.py)、[复制](../src/scale_from_zero/replication.py)、缓存、负载均衡、GeoDNS以及相关测试，结合实际SQLite受控竞态核对，不以调用图代替执行证据。

1. **租约检查和数据提交不能分开。** 旧代码先`assert_held`，再获得资料SQL写入权，最后独立publish。暂停旧写后让租约过期并提升另一个holder，旧主仍能提交不同资料/幂等结果/outbox，只在publish时返回403。现在`Fence.writing()`持有共享Fence写事务，范围覆盖资料事务和游标发布；在资料事务内重新核验当前时钟。SQL等待期间过期会整体回滚；正在提交的写会阻挡提升，提升醒来后仍须满足最新游标。
2. **输入错误不能破坏已有缓存。** 旧`put`先移除旧项，再校验调用方TTL；同值设置负数/非有限/布尔TTL会报错并丢掉旧值。现在TTL在任何缓存修改之前校验，值、字节统计和淘汰计数保持原样。

[租约回归](../tests/test_leadership_safety.py)覆盖等锁过期、提交中提升、普通异常回滚；[缓存回归](../tests/test_cache_safety.py)覆盖五种无效TTL。旧实现先复现失败，修复后执行完整测试和实际网络示例；结果见[本轮审查](chapter02-review.md)与[验证报告](../benchmarks/REPORT.md)。

## 不能据此宣称完成的生产能力

本地默认1/8/10进程阶段未挂载Fence；显式提升仅迁移资料写入，不迁移会话、幂等和任务。Fence与资料是两个SQLite文件：正常异常可回滚，进程崩溃在资料提交与Fence提交之间仍可能留下未确认写；必须保留原盘恢复，不是跨库原子事务、共识或生产数据库HA。

GeoDNS只是健康目录，edge只是本机CDN模型；没有真实地理DNS、全球POP、跨主机复制、自动伸缩、在线分片迁移或百万用户容量证明。HAProxy和Redis各解决部分组件问题，并不能拼成这些未实现的整章部署能力。
