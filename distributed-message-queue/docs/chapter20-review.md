# 第20章核对：分布式消息队列

## 2026-11-09 分块追赶（P3部分完成）

原书要求follower从leader拉取并追上ISR。旧实现对任何不合格副本都读出`floor..hwm`
全部记录、在协调器内存中拼成整份快照再`/install`新代；超过10000条/24MiB即放弃，
因此落后2万条的副本**永远追不上**，且每轮仍白读1万条。

现改为增量分块追赶（`coordinator_replication.py`的`_resume_point`/`_catch_up`/
`_committed_hash`）：
- `/status`新增可选字段`tail_hash`、`checkpoint`、`checkpoint_hash`（旧字段不变，
  旧协调器忽略；不返回这些字段的旧broker自动退回快照）。
- 续传点取副本尾部（不超过hwm）与其持久化已提交检查点中、哈希与元数据一致的最高者。
  尾部分叉（未提交写入与新leader不同）时退到检查点，首块`/sync`的`align`截掉其后
  后缀，与普通同步截暂存尾部同一路径。两者都不匹配或副本base高于floor才走`/install`。
- 每块500条：从leader读取并对照元数据哈希校验，以`committed=上块末offset`、
  `committed_hash=上块末hash`作为普通`/sync`发送；副本`_prepare`重验offset连续与
  哈希链，逐块fsync并持久化检查点。协调器任一时刻只持有一块，不再物化整段落后量。
- 每次复制调用每副本预算10000条/24MiB；耗尽时本轮不计入该副本（不影响多数派），
  下轮从其持久尾部继续。无新增依赖，未新增broker路径。

`/status`全量`scan()`保留：增量校验只能覆盖新写入字节，无法发现旧帧损坏，达不到
原“选主前完整性”保证。分块走`/sync`而非`/status`，故全量扫描仍是每轮每副本固定
两次，不随块数增加。代价如下实测所示仍是主要耗时，属已记录边界。

临时测量（WSL2，Python 3.14，进程内3 broker，副本停机期间写入、重开broker模拟重启，
每轮produce 100条约150字节记录，不入库）：
| 落后量 | 旧（HEAD+今日log/transport） | 新 |
| --- | --- | --- |
| 5000 | 1轮，全量`/install`，峰值Python内存28.1MB | 1轮，13次`/sync`，峰值9.3MB |
| 20000 | 6轮仍未追上（每轮约5.6s，超快照预算） | 3轮追上，共20.9s，其中`/status`扫描15.4s |

20000条场景峰值内存（新36MB/旧33MB）主要来自同进程broker的全offset索引与扫描，
非协调器传输缓冲。另以临时脚本验证：落后且尾部有5条伪造分叉记录的副本截回检查点后
分块追平，尾哈希与leader一致、零次`/install`。

验证：既有测试`-W error`下 **121 passed**（未改测试或基准代码）。剩余P3：完整动态ISR、
zero-copy及大保留量索引边界；低于保留floor的副本仍需全量快照。

## 2026-11-09 效率复核

核实一处每批复制的重复全量扫描：协调器每次produce对每个broker先`/status`再`/sync`，
`/sync`经`synchronize`→`PartitionLog.align`在持久化epoch后无条件调用`_scan()`，
重读全部保留段并重做CRC、JSON解析和SHA-256链校验，即使没有需要截断的暂存尾部。
批处理成本因此随保留日志长度线性增长。

修复只改`log.py`的`align`：仅当`committed+1 < next`时截断，并直接删除内存索引中
截断点之后的条目、截短该段`.idx`（新增`_truncate_index`），不再全量重扫。epoch
仍先原子持久化再截尾，I/O失败仍使日志对象失效。`/status`的全量扫描是“完整性优先”的
有意选择，无法以增量方式保留同等保证（增量只能覆盖新写入字节，不能发现旧帧损坏），
故保持不变；打开、恢复及retention仍全量扫描。

临时测量（WSL2 Linux，Python 3.14.4，不入库）：单分区保留2万条约200字节记录，
每批10条，20批取均值。旧代码`/sync` **361.3ms/批**，新代码 **0.7ms/批**；
`/status`保持约310–358ms/批。截尾后另行重开日志，验证`.idx`与内存索引和全量重建完全一致。

验证：`-W error`加`-o asyncio_default_fixture_loop_scope=function`（否则pytest-asyncio
弃用警告使收集即INTERNALERROR）。修改前后均为 **115 passed、6 failed**，失败集合相同，
均为既有测试中`HTTPError`未关闭引发的ResourceWarning；不加`-W error`时修改后
**121项全部通过**。详见[验证报告](../benchmarks/results.md)。

## 2026-11-04 源码复核

重读本章并结合CodeGraph追踪存储、复制、组、生产者、HTTP及生命周期。
分别检索并阅读Kafka批处理/重试和RocketMQ长轮询/过滤源码，见[固定版本对照](open-source-review_cn.md)。
核心功能仍按下表覆盖，生产扩展保持未完成。

本轮修复一处并发准入缺口：空长轮询能占满全部HTTP handler，使写入及心跳无法进入。
正等待fetch/group-fetch现在共享floor(3×concurrency/4)个名额，超额503；名额直到
响应及清理结束才释放。默认32个handler允许24个长轮询；concurrency=1拒绝正等待，
零等待仍可用。此措施只约束已解析长轮询，不保证所有慢请求的公平性。

先用真实HTTP/受控等待服务复现两个入口的缺口，再增加5项回归。最终
**121项通过，35.634秒，无跳过，使用`-W error`**；最终测试调度修订后HTTP专项
27项再次通过。两个真实独立进程示例通过，120条消息全部提交并读回、零错误。
最新命令、图分析及实测见[验证报告](../benchmarks/results.md)。

过滤位置进一步明确：协调器按消息tags过滤后返回客户端；broker到协调器仍传完整帧
并校验hash，不宣称节省内部RPC的payload。学习指南补充长轮询准入与释放图。

## 2026-10-26 历史核心审查

核对日期：2026-10-26（Asia/Shanghai）。依据根目录
[《System Design Interview.md》](<../../System Design Interview.md>)第8020–8996行。
本轮保留标准库、独立HTTP broker磁盘和单个持久协调器的参考架构。

## 章节映射

| 原书内容 | 可执行实现 | 具体边界 |
| --- | --- | --- |
| 主题、分区、FIFO、重复消费 | 分区offset、retained log、独立消费组游标 | 只有分区内顺序；同名group在不同topic独立协调 |
| 文本消息、键、时间、校验 | 共享消息契约、CRC帧及SHA-256链、topic/partition由请求上下文确定 | JSON信封有编码和复制成本；未实现零拷贝 |
| 分段追加与旧段回收 | 活跃段旋转、offset索引、恢复/校验、逻辑floor和完整旧段删除 | 全offset内存索引；活动段可能保留较久，非无限容量 |
| 批处理的延迟/吞吐取舍 | send_many分区批、buffered的batch_size/linger、按段data/index批刷盘、批拉取 | buffered只有一个自有worker，跨分区批次非原子事务 |
| 生产者路由 | 每条key CRC32取模、无key随机、显式partition覆盖、扩容后重新取元数据 | 仍经协调器转发复制，未实现客户端直连leader |
| 幂等重试与身份隔离 | producer epoch、连续批sequence、指纹回执、未知结果保留原批次并retry_pending | 新epoch不去重；客户端恢复状态不跨进程自动持久化 |
| 拉取与长轮询 | fetch/group-fetch、0..5秒等待、发布通知/100ms兜底 | 等待释放分区和SQL锁；断连不能立即取消已接受业务/磁盘工作 |
| 组成员、退组、故障重平衡 | join/heartbeat/leave、租约、generation/session fencing、轮询分配 | 服务器直接生成计划；没有另建分布式组协调器或客户端组leader协议 |
| 消费状态/主题元数据 | SQLite WAL/FULL、短写事务与一致性读快照 | 单写者、单协调器所有权；未部署ZooKeeper/etcd共识 |
| leader/follower、选主与提交水位 | 独立HTTP复制、已提交前缀hash校验、epoch、故障换主、pending日志 | 协调器推动复制；全量catchup受预算限制，未实现后台增量pull |
| ISR/ACK取舍 | all要求配置broker多数确认，leader只暂存，客户端一次发送适配器 | 非Kafka动态ISR/任意K配置；leader确认暂不可消费；适配器仍使用HTTP |
| 横向扩展 | 多生产者/消费组、不同分区协议并行、expand原子增加分区并改变组generation | 不迁移旧数据；取模变化后同key新旧分区无整体顺序保证 |
| broker加入/下线与分区减少 | 本轮未实现 | 仍是静态broker集合；未实现追平后迁移副本或退役分区保留期协议 |
| 至少一次/至多一次 | 后提交/响应前提交游标，重投递/允许丢失语义明确 | 仅拉取与确认约定，不替调用方保证外部业务处理 |
| 恰好一次 | process以txid将多个输出可见水位和输入游标在同一元数据事务提交 | 仅队列边界；外部HTTP/邮件/数据库、新txid或显式reset不在边界内 |
| 元数据标签过滤 | 1..16个any-of标签，扫描位置推进且可提交；不解析payload | tags省略/None不过滤，空筛选列表拒绝；过滤策略改变不能找回已提交跳过项 |
| 延迟/定时消息 | delay_seconds或deliver_at，持久not_before，延迟头阻塞后续以保序 | 非时间轮/独立调度主题；保留策略独立，可在到期前过期 |
| 重试/死信及历史恢复 | 原子重试/死信、有限流式本地归档、预验证后批回放 | 非云分层存储；归档不pin保留范围，回放新offset且非恰好一次恢复 |

## 工程与并发

Coordinator门面将消费者组、复制、保留、schema和验证分开，客户端与服务器复用messages契约。
固定条带操作锁按顺序获取，跨分区process一次获取源/目标/事务键，避免嵌套锁顺序反转。
网络请求在SQLite事务外；同分区协议串行，条带碰撞仍会串行无关操作。producer epoch变更
与该生产者的提交有共同隔离边界，因此同一producer身份跨分区发送仍会在服务器串行；
不同producer身份的无碰撞分区可并行。元数据事务仍只有一个写者，未声称完全无锁或线性扩展。

Broker仅用短锁协调缓存与同分区冷加载，不在全局锁中做磁盘操作。PartitionLog内部串行化，
完整冻结/校验批次后才改磁盘；data与index按段批刷盘，读取复用句柄。epoch先持久再截尾，
记录已观察committed checkpoint并拒绝回退。I/O失败/完整损坏后fail closed，重开验证后恢复。
快照在新代完整校验后发布原子指针，清理失败不撤销已经发布的成功代。

保留每轮每分区最多处理4096条前缀；远端物理回收计划进入持久journal，失败跨重启重试。
默认14天或每分区8MiB先到者。旧receipt清理后sequence水位仍阻止旧序号再次产生消息。
事务回执/epoch等元数据仍需容量与GC策略。

HTTP默认32个处理名额、10秒socket总预算和128 backlog，过载503；鉴权先于正文，拒绝
重复/歧义framing、重复JSON字段与非有限数，RPC不走环境代理或重定向。自有处理线程、
deadline watcher、维护worker均有关闭/排空；维护失败通过日志与health/metadata可见。
DNS、SQLite、CPU工作不能被socket截止时间强杀，容量保留到实际退出。

Producer冻结重试内容，同分区sequence串行、不同条带可并行。buffered限制待完成数目，
Future暴露成功/失败，flush/close排空；取消只在发送前有效。一次发送适配器使用有界执行器，
Client.close等待接受任务，并拒绝在自身worker回调中同步join。进程强制退出仍可能丢失未确认发送。

## 验证与图分析

修改前基线：15项测试通过，27.598秒，ResourceWarning作为错误。
最终完整 **116项通过（34.735秒），无跳过和ResourceWarning**；真实HTTP独立进程
测量120/120条消息提交并读回、75.79条/秒、零错误。最终回归、独立进程功能演示和
本轮性能原始数据见[验证报告](../benchmarks/results.md)。
独立进程场景使用三个分离磁盘broker、一个协调器和三个仅转发/切断TCP的代理；替换RPC的
确定性故障测试另行标注，不冒充真实网络。功能演示见[chapter_demo](../examples/chapter_demo.py)。

code-review-graph已检查最小上下文、调用方和显式限定本项目的影响范围。初始索引对
Coordinator.produce的限定调用方查询返回空，并提示Python动态dispatch可能漏边；它不是没有调用方
或没有测试的证明。全量重建548文件、6970节点、62814边，无构建错误，最终增量刷新也成功。
七个既有核心文件影响分析为102个直接改动节点、24个受影响节点、另外5个文件；
1933个调用未解析，边输出有截断。新建未跟踪producer.py明确未入索引；本轮其他新模块
以源码和执行交叉核实，不能将图统计视为完整覆盖。其他章节脏改动仍混入全库图上下文。

## 未完成的生产能力

共识元数据与协调器HA、动态broker/副本迁移、分区缩容退役、增量catchup、零拷贝、完整动态ISR、
多topic组协调、TLS/topic ACL/配额、云归档、WAN/多地域镜像、备份恢复、持续压测和掉电验证
均未完成。Windows目录fsync能力、分段和指针在所有文件系统上的掉电持久性不能由进程测试推断。
这些边界不因本机零错误或单次吞吐测量而改变。
