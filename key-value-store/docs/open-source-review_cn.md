# 第7章：原书、开源实现与本地代码对照

复审日期：2026-10-31。原书范围为根目录 `System Design Interview.md` 第1735–2213行。
本页记录本轮结果；[前次审查](chapter07-review.md)保留2026-10-27的历史证据。

## GitHub参考

| 项目与源码 | 参考内容 | 本地取舍 |
|---|---|---|
| [Voldemort](https://github.com/voldemort/voldemort)；[并行读请求](https://github.com/voldemort/voldemort/blob/master/src/java/voldemort/store/routed/action/PerformParallelRequests.java)、[VectorClock](https://github.com/voldemort/voldemort/blob/master/src/java/voldemort/versioning/VectorClock.java) | Dynamo风格的分布式KV；非阻塞副本请求、版本比较/合并、故障响应处理 | 对照副本并发与因果版本；运行路径继续使用现有ReplicaCoordinator。所读并行读实现等待preferred请求的latch或超时，并不等于达到required立即返回；本地已有提前仲裁与有界后台任务，无需照搬Java流水线。 |
| [LevelDB](https://github.com/google/leveldb)；[DBImpl::Write / MakeRoomForWrite](https://github.com/google/leveldb/blob/main/db/db_impl.cc)、[存储与恢复说明](https://github.com/google/leveldb/blob/main/doc/impl.md) | 单机LSM存储；Sync失败后记录错误，后续写入停止；恢复日志后建立可继续写入的存储状态 | 借鉴不确定磁盘状态下停止写入的原则，修复本地WAL残尾及I/O失败后的准入；保留现有文件格式，不增加依赖或移植LevelDB的分层存储。 |

Voldemort的README明确说明已停止开发，适合作为历史实现阅读。LevelDB属于本地存储组件，不能替代分布式仲裁、Gossip或hinted handoff。本轮阅读上述源码后独立修改Python实现，没有复制其代码。

## 章节覆盖与真实运行路径

| 原书主题 | 本地入口 | 状态与边界 |
|---|---|---|
| put/get、小对象 | `KVClient` → `KVNode` | 支持；另有墓碑删除。键最多256字节，值最多10KiB，空bytes不是删除。 |
| 分区、虚拟节点、异构容量 | `ReplicaPlacement`、兄弟项目`ConsistentHashRing` | 支持不同物理副本、权重及故障域配置；单个热键仍不能靠虚拟节点拆开。 |
| N/W/R、异步副本、CAP取舍 | `ReplicaCoordinator._write_admitted/_collect` | 并发扇出，达到仲裁返回，后台任务有界并在关闭时排空。分区后无法满足仲裁会失败；W+R>N只表达稳定副本集合上的交集。 |
| 向量时钟、siblings、客户端合并 | `ReplicaRecords`、`reconcile` | 支持因果支配、冲突并存、时钟裁剪及显式解决；裁剪会损失部分因果历史。 |
| Gossip与短暂故障 | `GossipProtocol`、`HintJournal`、`HintedHandoffManager` | 支持故障怀疑、sloppy quorum和SQLite持久补送；不是成员拓扑共识。 |
| 永久故障修复 | `AntiEntropyManager`、`MerkleTree` | 分层比较、差异桶分页、携带墓碑修复；持续变化会中止本轮并重试。 |
| 写路径 | `StorageEngine.put/delete` → `WriteAheadLog.append` | fsync后发布MemTable，达到阈值生成SSTable；本轮补齐残尾恢复与I/O失败后停止写入。 |
| 读路径与磁盘 | `StorageEngine.get`、Bloom filter、SSTable | 内存与候选表按物理记录时间选择；Bloom只排除不可能存在的键。整个SSTable缓存和压缩仍非流式。 |
| 去中心化与跨机房 | 节点对等协调、`node_data_centers` | 对等节点与故障域放置已实现；真实WAN、机房灾难、TLS/认证未验证或未实现。 |
| 根据流量自动增减服务器 | 成员发现、反熵补齐是已有基础 | 自动资源供给、安全缩容屏障、拓扑epoch/共识未实现。当前没有云资源控制面，不增加一个只有模拟动作的“自动扩缩容”接口。 |

CodeGraph沿`KVNode`实际装配核对了协调、版本记录、存储及修复路径。
`RequestCoordinator`在节点装配中委托给`ReplicaCoordinator`；早期`QuorumManager`
独立组件仍使用gather等待全部结果，不能据此推断节点运行时会等待最慢副本。
图分析用于定位调用与影响范围，行为结论以回归测试、真实RPC和进程验证为准。

## 本轮修复的失败序列

原先日志可能成为 `完整A | 不完整B | 已确认C`。重放在B处停止，首次恢复时
A仍可读取，但继续写入的C会在下一次重启丢失。之前的测试只检查“能读到A”，
没有检查“恢复后再写，下一次重启还能读到C”。

现在在WAL互斥锁内定位最后一个完整帧边界，截去不完整后缀并fsync，然后才允许
追加。完整帧的CRC失败仍沿用跳过策略；这不是任意磁盘损坏的纠错或重新同步。
write、fsync、truncate或恢复I/O抛出OSError后，实例拒绝后续append/truncate，
保留数据供恢复。简单close不解除限制，成功replay才解除；调用方必须应用恢复
条目后再服务，节点通过重新启动存储引擎完成这一步。错误写入可能已有完整记录，
恢复后可能出现该记录，失败不表示回滚。

重放与追加、截断、关闭使用同一锁。重复取消恢复任务也会等待磁盘线程结束，
避免后台截断与后续追加交错。WAL二进制格式与公共API保持兼容。
示例同时修正了多节点独立存储路径、仲裁交集措辞和正常关闭被误称为崩溃的问题。

## 验证

- 修改前全章417项通过；新增[11项回归用例](../tests/test_wal_failure_review.py)在旧实现全部失败，修复后全章**428项通过，19.43秒**，`-W error`，无测试跳过或警告过滤。
- 新用例覆盖6种残尾边界、部分write与fsync失败、16个待写请求被拒绝、truncate失败、重复取消下的锁归属，以及恢复后连续异常停止。
- [三进程验证](../benchmarks/wal-recovery-2026-10-31.json)：首进程fsync已接受记录并构造2字节残尾，第二进程恢复后写入，第三进程读到两条记录；各进程均使用`os._exit`，没有MemTable flush。这验证进程退出，不代表物理断电验证。
- `examples/demo.py`与`examples/repair_demo.py`通过。后者用真实loopback gRPC，在64KiB通道中用5页传输196608字节，保留墓碑且收敛；第二轮0数据页。
- [性能原始数据](../benchmarks/open-source-review-2026-10-31.json)与[测量条件](../benchmarks/REPORT.md)保留本轮结果；未隔离主机噪声，不宣称吞吐提升。

自动伸缩、安全缩容、拓扑共识、大数据有界内存恢复及真实跨地域容灾仍是明确缺口。
本轮完成的是章节核心机制复审及已复现的持久性修复，不能据此宣称全部生产部署能力完成。
