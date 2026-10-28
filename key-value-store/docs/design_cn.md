# 第7章运行设计

依据原书1735–2213行，见[章节覆盖](chapter07-review.md)和[实测](../benchmarks/REPORT.md)。

## 职责与本地提交

协调器处理quorum，ReplicaPlacement统一副本/故障域，ReplicaRecords在条带锁
前冻结输入，合并因果siblings并把值/时钟/墓碑存入同一记录。engine负责WAL、
内存表和SSTable；网络修复放在cluster/anti_entropy.py与network/repair_service.py，
原cluster/repair.py保留ReplicaRepair导入并负责SQL hint。

内部存储时间取max(墙上时间,nextafter(上一次物理时间,+inf))，与每个版本自身
的因果元数据分离。读取/恢复在候选层中选择每键最大时间，避免回退和文件创建
顺序导致旧值覆盖，代价是增加读工作。原始StorageEngine仍接收调用者时间戳，
records层为自己的写入保证单调。写入后flush失败时按可见数据刷新Merkle索引，
同时把失败返回调用者，不把失败解释成回滚。

async_work.complete屏蔽一个本地提交的取消并等待其结束，重复取消也不提前
释放锁。WAL、内存和Merkle完成后才暴露取消；SQL线程同样排空。真实提交
错误优先报告。不会把整个网络修复轮次屏蔽取消；磁盘阻塞可能延迟关闭。

SSTable临时文件fsync后原子替换发布，支持的平台同步目录，之后才截WAL；
压缩输出持久化后再淘汰输入。淘汰失败记录日志，旧文件可能残留，但读取
仍按时间选胜者。停止即使flush失败也关闭WAL；损坏最终SSTable拒绝启动，
保留恢复数据。Python没有可移植Windows目录fsync，未进行物理断电验证。

## Merkle与分页修复

桶摘要保持排序且带长度的键/值摘要，完全二叉堆缓存旧格式父哈希；单桶更新
只重算O(log B)条根路径，另需桶内O(K)摘要/有序插入。快照保存不可变堆引用。
桶数为1..65536的2次幂；key_page用二分游标取得有界切片，不逐页全量排序。
该索引用于单event loop，不保证任意跨线程调用。

MerkleInspect每次接收最多64个唯一堆索引，从根开始，只继续比较差异子树。
桶数不同返回FAILED_PRECONDITION，预期root变化返回ABORTED。
RepairBucketPage携带root、桶号、排他after_key、最多128条及字节预算；
服务端预算不超过2MiB且小于通道限制。每次await读取后复核root，单条过大
明确RESOURCE_EXHAUSTED。客户端检查节点完整性、排序/桶归属及游标前进。

每节点串行修复轮次，副本放置取本地快照；先拉完远端差异页，再推送本地，
避免推送改动远端root使后续拉取失效。并发变更时已合并数据有效，下轮幂等
重试；没有全局一致快照/回滚。持续写入可令轮次反复中止；只限制逐RPC超时，
尚无全轮硬截止或持久化桶内续传游标。

树覆盖所有本地记录，未按每对节点的共有副本范围分树。N小于成员数时，即使
负责的记录已收敛，仍可能反复比较非共有桶；修复过滤放置且不删除非归属数据。
全局root不等于全群线性一致性证明。

## hint与边界

hint锁前冻结/校验，同对象投递轮次互斥，ACK按原id/目标/键/blob删除，防止
另一个journal实例触发ID复用后被旧ACK误删。保留分页公平性；SQL取消排空。
没有磁盘配额、自动合并积压或DLQ控制器。

W+R>N只表示quorum交集，sloppy副本、并发siblings和成员变化不提供共识；
专用Replica RPC避免协调器递归复制。配置权重/故障域不等于自动供给或容灾。
自动扩缩容/安全下线屏障、拓扑共识、TLS/认证、真实跨地域、流式压缩和有界
磁盘缓存仍未完成；当前SSTable会缓存整个文件，启动/重建扫描全量数据。


修复页另核对记录与索引摘要，拒绝尚未完成索引发布的可见提交。
