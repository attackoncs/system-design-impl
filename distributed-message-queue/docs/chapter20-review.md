# 第20章核对：分布式消息队列

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
