# 第18章：开源源码对照与实现核对

日期：2026-11-02。重新阅读根目录《System Design Interview.md》6726–7293行，
范围为Nearby Friends，不是静态商家搜索。核心是授权、位置时效与变化分发。

## GitHub开源参考

通过GitHub分别搜索位置记录与限时位置共享项目，实际阅读以下固定提交的源码。
参考它们的职责划分与边界，没有复制上游代码、添加依赖或把不同权限模型直接套用。

| 项目与固定版本 | 实际阅读源码 | 可参考机制 |
| --- | --- | --- |
| [OwnTracks Recorder](https://github.com/owntracks/recorder/tree/026160b4dfb49915fdaec4ead4dd157d87344f3c) | [http_ws_push_json](https://github.com/owntracks/recorder/blob/026160b4dfb49915fdaec4ead4dd157d87344f3c/http.c#L195)、[send_last](https://github.com/owntracks/recorder/blob/026160b4dfb49915fdaec4ead4dd157d87344f3c/http.c#L294)、[LAST命令入口](https://github.com/owntracks/recorder/blob/026160b4dfb49915fdaec4ead4dd157d87344f3c/http.c#L1409) | 实时JSON分发与最近位置播种分开；向WS连接发送前按user/device及X-Limit字段过滤。LAST读取最近位置后使用相同分发路径。 |
| [Hauk](https://github.com/bilde2910/Hauk/tree/b05c40f9fc819717aa4ec7f92976e80bb238a922) | [post.php](https://github.com/bilde2910/Hauk/blob/b05c40f9fc819717aa4ec7f92976e80bb238a922/backend-php/api/post.php)、[fetch.php](https://github.com/bilde2910/Hauk/blob/b05c40f9fc819717aa4ec7f92976e80bb238a922/backend-php/api/fetch.php)、[会话保存/结束](https://github.com/bilde2910/Hauk/blob/b05c40f9fc819717aa4ec7f92976e80bb238a922/backend-php/include/inc.php#L626)、[addPoint/getPoints](https://github.com/bilde2910/Hauk/blob/b05c40f9fc819717aa4ec7f92976e80bb238a922/backend-php/include/inc.php#L750) | 上报依附有效共享会话；分享/会话按到期时间保存在Memcached。fetch使用链接ID并支持since增量，点缓冲限制数量，结束会话清理关联分享。 |

具体用于本项目审查的依据：

1. **初始化和实时变化要能衔接。** OwnTracks的LAST与实时分发分开，提示检查
   本项目连接快照、首条位置、重连后播种与后续delta；Pub/Sub不能代替初始事实。
2. **过滤必须与实际权限模型一致。** OwnTracks的user/device字段过滤不是双向
   好友确认；Hauk的链接ID访问也不是好友ACL。本地必须继续复核好友、双方共享
   和TTL，不能因为上游已有连接/链接就认为所有位置都可发送。
3. **过期与重放不是同一个概念。** Hauk按会话到期及since过滤点，本地按服务端
   received_at起算600秒不活跃TTL、用户timestamp拒绝乱序，并不回放丢失的频道
   消息。历史是另行同意的持久数据，不能把“缓存最近点”写成历史授权。
4. **状态需要有限保留。** Hauk点缓冲有数量上限。本地分别限制帧、同步提交、
   连接池、broker客户端和修复批次；SQL历史按保留期限清理。资源边界要逐层说明。

两套项目是可运行的位置记录/共享实现，不是本章“WS+Redis按好友频道路由”的
完整复刻。OwnTracks选读的C模块没有证明其满足本地双向好友策略；Hauk的PHP
轮询/共享链接、可选加密也不同于本项目。未部署或压测上游项目，不比较性能。
根开源索引中原有FriendMap业务项目条目保留，本轮实际阅读的是上述两套源码。

## 章节覆盖与差距

逐项映射见 [章节记录](chapter18-review.md#原书与实现映射)。本轮没有确认需要
修改的源码缺陷，保持现有models/routing/schema/protocol/work与业务模块边界。

| 书中要点 | 当前机制 | 限制 |
| --- | --- | --- |
| 5英里可配、直线距离、距离/更新时间 | 默认8046.72m、最大100km，稳定球面距离；快照与enter/update/leave | 非道路距离，无原生手机界面/头像资料服务 |
| 30秒上报、10分钟不活跃隐藏 | 建议客户端30秒上报；收到写锁后校验时间、采样received_at并起算600秒TTL | 非服务端强制上报；断线保留到TTL到期 |
| 好友与共享 | 双向请求/接受/移除、5000好友上限、双方共享、Redis操作后最终SQL复核 | 最终读后仍有在途帧竞争；不能收回已发送数据 |
| 初始化、每页20和实时更新 | 连接快照、首条location、实时offset分页，分页不覆盖完整delta基线 | 页面随移动漂移，客户端按ID归并；不是历史版本游标 |
| Redis位置缓存、TTL、批量读取 | 真实Lua版本保护、剩余TTL、MGET及每批128条修复，有预算和池上限 | Redis为镜像，命中仍两次SQL读取；非原书Redis权威快速路径 |
| 用户频道、离线好友订阅、丢点恢复 | 提示只含ID；订阅全部好友，重连/每秒对账获取当前集合 | Pub/Sub可丢，不是可重放日志；每会话扫描全部好友 |
| 一致性哈希、服务发现、节点变更 | 每broker64虚拟点、1..32节点、SQL配置epoch CAS，受影响订阅重建 | 未部署etcd/ZooKeeper；位置缓存本身未物理分片/主备 |
| 位置历史与横向扩展 | 独立历史许可、本人读取、默认24h保留、关闭删除 | 用户/好友/当前位置恢复/历史仍在同机SQLite，非Cassandra或分片集群 |
| WS摘流、低延迟和资源 | 停止准入、有限同步工作、发送总期限、取消后保留I/O所有权并排空 | 无云LB自动摘流；应用会话限制不等于TCP/握手硬配额 |
| 附近陌生人、Erlang | 设计中记录为额外功能与替代路线 | 当前不实现陌生人位置API或第二套BEAM引擎，不扩大好友权限 |

物理分片、Redis主备、SQL HA、共识发现、跨地域、TLS、生产身份和持续容量验证
仍未落地；千万连接、334K上报/秒和约14M转发/秒是书中估算，不能由本轮实验证明。

## CodeGraph工程复核

读取Store/Backend/Session/WorkPool、领域校验、协议和Ring的当前源码与调用关系：

- Store：SQL写锁后做时间窗口、共享与用户级顺序判断；当前位置、revision、last_ts
  和已授权历史同事务。reports一次采样截止时间并一致读；关闭共享删除当前行，
  再开共享不复活旧位置，后台sweep失败可重试。
- Backend：Redis修复在最终授权SQL之前；复核双方位置、TTL、好友与共享，按
  最新坐标算距离。Redis失败不放大成每好友一次超时，broker使用租约防止活跃淘汰。
- Session：好友/epoch变化重建订阅并标dirty；周期对账恢复漏提示。增量leave只含ID，
  分页不覆盖完整基线；整批多帧共用发送预算，慢消费者不无限占用发送时间。
- WorkPool：最多workers个同步任务提交；取消等待者仍等待底层I/O退出才释放容量，
  关闭排空。协议拒绝重复字段、非有限值、未知业务字段、二进制和过大输入。
- Ring：自有不可变节点及token，一致哈希按发布者ID路由；URL不能覆盖资源预算。

图是导航依据，动态调用和索引覆盖有局限；实际运行用于验证结论。本轮未修改
生产或测试源码，也未增加镜像实现的重复测试。

## 真实验证与复现

完整标准库严格回归 **76项通过（28.047秒），无跳过**：9项核心、18项Store/Ring、
17项Backend、23项协议/会话/工作池、4项凭据、5项真实Redis集成。包括真实写锁
等待、历史故障回滚、延迟缓存期间撤权/TTL/移动、乱序、Lua版本/剩余TTL、
连接容量、取消排空、分页增量及跨WS节点重连/重订阅。受控后端与故障注入和
真实Redis/WS进程实验分别标明，不把前者当成真实基础设施故障演练。

环境：Windows11 build26100/Python3.13.9，websockets16.1.1、redis-py6.4.0，WSL
Ubuntu-26.04的真实Redis8.0.5。复用已有依赖/缓存程序，没有安装下载服务。每次
实验使用独立密码/run ID并停止自有进程，日志/SQLite保留于忽略的lab目录。

先按README配置实际Redis路径，在项目目录运行：

```powershell
python -W error -m unittest discover -s tests -v
python -W error examples/network_demo.py
python -W error tools/benchmark.py --connections 20 --updates 10 --output benchmarks/review-2026-11-02.json
```

本轮路径指向已缓存的Redis程序；应用不导入其他章节业务代码。实际网络实验使用
允许本机回环与WSL的测试进程，临时路径独立于共享服务，退出0。

示例运行2个Redis、2个独立WS进程，跨日期线距离222.39016046812768米，跨节点
入圈346.688ms，提示发布成功，关闭共享后授权快照为0。扇出21连接（20观察者）和
10次上报，**预期/实际200/200次投递**；端到端P50/P95为105.950/146.864ms，
最大173.977ms。按匹配revision的delta观测，包含SQL/Redis/对账/WS与客户端调度，
不单独计数Pub/Sub消息。测试、示例和测量顺序运行，无提速或生产容量声明。

[原始记录](../benchmarks/review-2026-11-02.json) · [测量方法](../benchmarks/results.md)

已同步中英文README、需求、设计、任务、章节记录、根进度与开源索引。学习指南
将“位置加速”澄清为镜像，补充两次SQL读取的微观解释、第三幅流程图和源码入口。

最终静态核验：19个项目Python文件通过3.10语法解析，源码Ruff E9/F通过；16份
文档的510个本地链接通过，27份学习指南生成一致，3幅Mermaid图实际渲染通过，
限定范围git diff --check通过。源码和测试目录没有本轮差异。
