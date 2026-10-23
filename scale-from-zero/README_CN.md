# 第2章：从零扩展到百万用户

通过可运行实验展示从单机到无状态Web层、缓存、读副本、数据分片和异步任务的演进。**各服务使用真实本机HTTP，但全部位于单台主机，不宣称已承载百万用户或实现多地域高可用。**

| 阶段 | 进程数 | 组件 |
|---|---:|---|
| single | 1 | HTTP应用、SQLite资料/会话及同进程后台消费线程 |
| balanced | 8 | 负载均衡、2个无状态Web、主库/读副本、缓存、worker、本地边缘缓存模型 |
| sharded | 10 | 相同Web/缓存/任务/边缘层，主库/副本扩展为两组 |

边缘服务是明确标注的本地CDN缓存模型，不是真实CDN或GeoDNS。数据库读副本只复制资料变更，不具备直接提升为主库的完整状态。

## 运行

Python 3.10及以上，仅标准库。本目录PowerShell执行：

```powershell
$env:PYTHONPATH = 'src'
New-Item -ItemType Directory -Force .runtime/tmp | Out-Null
$env:TEMP = (Resolve-Path .runtime/tmp).Path
$env:TMP = $env:TEMP
python -m unittest discover -s tests -v
python examples/network_demo.py
python examples/network_demo.py --stage sharded
python tools/benchmark.py --requests 30 --clients 4
```

11项测试含4项独立进程HTTP测试。演示依次启动1/8/10进程，两个合成用户登录/写入资料，验证同会话跨Web节点、读副本/缓存及任务处理。退出只清理自己启动的进程和临时数据库。三种阶段各自使用新数据，**不是在线数据迁移步骤**。

持久运行先执行 `python tools/init_config.py --stage sharded`。查看 `.runtime/private-config.json` 的 `services`，为每个节点开独立终端，设置 `PYTHONPATH=src`，例如：

```powershell
python -m scale_from_zero.cli primary0 --config .runtime/private-config.json
python -m scale_from_zero.cli replica0 --config .runtime/private-config.json
python -m scale_from_zero.cli primary1 --config .runtime/private-config.json
python -m scale_from_zero.cli replica1 --config .runtime/private-config.json
python -m scale_from_zero.cli cache --config .runtime/private-config.json
python -m scale_from_zero.cli web0 --config .runtime/private-config.json
python -m scale_from_zero.cli web1 --config .runtime/private-config.json
python -m scale_from_zero.cli lb --config .runtime/private-config.json
python -m scale_from_zero.cli worker --config .runtime/private-config.json
python -m scale_from_zero.cli edge --config .runtime/private-config.json
```

balanced省去primary1/replica1；single仅运行single节点。更换阶段应使用新的配置/数据库目录；生成器拒绝覆盖已有配置，已有数据节点拒绝直接更改分片数量或索引。凭据已加入Git忽略，但生成器不修改文件权限，请妥善保护。配置和监听仅允许回环地址。

## 资料与会话实验

使用配置中的 `entry` 地址，用户名/口令取自 `users`：

1. `POST /v1/login`，请求 `{"user":"alice_0","password":"<生成的实验口令>"}`，返回一小时会话。
2. 携带 `Authorization: Bearer <session>`，调用 `POST /v1/profile`：

```json
{"request_id":"write1","expected_version":0,"display_name":"Synthetic User","city":"Test City"}
```

3. `GET /v1/profile` 返回本人资料、版本、读取来源与Web节点；同一会话可跨Web节点使用。
4. `GET /v1/profile?consistency=strong` 绕过缓存/副本，读取主库。`?min_version=1` 只接受至少该版本的缓存/副本，否则回退主库；要求未来版本返回409。

资料字符串限制128字符，更新使用expected_version防止覆盖并发变更，request_id在用户内幂等。同键不同内容、旧版本写入返回409。成功请求重试返回原结果，可能早于当前最新资料；丢响应后重用原键。会话令牌以哈希形式存于用户所属主库，不在Web进程内；生成口令仅为实验身份，不是生产密码管理。

负载均衡采用轮询与请求时故障回退，GET及携带幂等键的资料POST可重试另一Web；登录POST不自动重试。这里没有后台健康探测/选主服务。

## 一致性与任务

默认读为最终一致：先32项LRU/2秒TTL缓存，再异步资料副本；副本不可用、记录缺失或低于min_version时读主库。缓存失败可绕过。写入先持久提交，再尽力失效缓存。

并发回填可能与失效竞态，副本长期滞后也可能让旧值再次进入缓存；**TTL限制单次缓存项寿命，不限制整体副本滞后**。需要新鲜度时选strong或min_version。会话校验仍依赖主库，因此资料副本不等于主库高可用。

资料、版本、幂等结果、复制变更与outbox任务在同一SQLite事务提交。读副本检查连续序号、校验和并去重，状态与游标原子更新；不复制会话、幂等索引或任务，不做自动主库提升。

worker使用持久任务、一秒租约与认领令牌；独立收件表按稳定事件ID去重。消费后丢确认会重试，但事件只记录一次。单个分片故障不会饿死其他分片任务。这里只记录模拟通知/审计事件，不发邮件或外部消息；采用至少一次投递、持续重试，没有生产消息队列/死信或网络恰好一次保证。

## 静态资源与观察指标

源站entry和edge均可读取 `/assets/app-v1.txt`、`app-v2.txt`，版本化路径避免原地覆盖资源；边缘TTL30秒。edge不可用时客户端可显式改读源站，未配置DNS或浏览器自动回退。

管理凭据访问各服务 `GET /admin/status`，可查看Web身份、负载均衡回退次数、主库资料读取次数、副本游标、缓存命中/淘汰、待处理任务及消费者唯一事件/尝试数。worker的 `POST /admin/control` 接受 `{"lose_ack":true}` 模拟消费后确认丢失，恢复时设false。这些是实验计数器，不是完整生产监控/告警。

## 验证与限制

详见[设计](docs/design.md)、[验证及实测](benchmarks/REPORT.md)和[原始样本](benchmarks/latest.json)。小流量下新增网络层可能更慢，实验观察其开销及主库资料读取减少，不预设分层一定提速。

固定SHA256用户分片，没有在线迁移。主库、会话库、负载均衡及各单实例服务仍有单点；主库停机影响该分片的鉴权、写入和强读。未验证跨机耐久性、自动主库切换、真实CDN/GeoDNS、多地域、百万用户负载、生产TLS/身份或自动伸缩。每主库限制1000条资料变更和1000个有效会话，保留完整历史。后续章节组件可用于深入学习，但未冒充已经集成为本实验的生产基础设施。

## Engineering review (2026-10-23)

Routing/replay policy lives in `balancer.py`; owned background operations and shutdown live in `workers.py`. Cache synchronization is internal, so direct reuse is thread-safe. Network I/O and cache copying do not hold routing/cache metadata locks. See [per-project review](../DEEP_REVIEW_PROGRESS.md) for findings, regression evidence and remaining limits.
