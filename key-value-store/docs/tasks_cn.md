# 任务清单：分布式键值存储

## 任务依赖关系图

```
Task 1 (项目脚手架)
  │
  ├──► Task 2 (存储层)
  │       │
  ├──► Task 3 (复制层)
  │       │
  ├──► Task 4 (集群层)
  │       │
  ├──► Task 5 (网络层)
  │       │       │
  │       ▼       ▼
  │     Task 6 (节点协调器) ◄── Task 2, 3, 4, 5
  │       │
  │       ▼
  │     Task 7 (客户端 API) ◄── Task 5, 6
  │       │
  │       ▼
  │     Task 8 (集成测试与基于属性的测试) ◄── Task 2–7
  │       │
  │       ▼
  └───► Task 9 (示例与文档) ◄── Task 7, 8
```

**依赖关系：**
- Task 1：无依赖（基础）
- Task 2：依赖 Task 1
- Task 3：依赖 Task 1
- Task 4：依赖 Task 1
- Task 5：依赖 Task 1
- Task 6：依赖 Task 2, 3, 4, 5
- Task 7：依赖 Task 5, 6
- Task 8：依赖 Task 2, 3, 4, 5, 6, 7
- Task 9：依赖 Task 7, 8

---

## Task 1：项目脚手架

- [x] 1.1 创建 `pyproject.toml`，使用 hatchling 构建系统
  - 文件：`key-value-store/pyproject.toml`
  - Python >= 3.9
  - 依赖：`grpcio`, `grpcio-tools`, `protobuf`, `consistent-hashing`（本地路径：`../consistent-hashing`）
  - 开发依赖：`pytest`, `pytest-asyncio`, `hypothesis`, `grpcio-testing`
  - 构建目标：`packages = ["src/kv_store"]`
  - Pytest 配置：`testpaths = ["tests"]`, `pythonpath = ["src"]`, `asyncio_mode = "auto"`

- [x] 1.2 创建目录结构
  - 目录：
    - `key-value-store/src/kv_store/`
    - `key-value-store/src/kv_store/storage/`
    - `key-value-store/src/kv_store/replication/`
    - `key-value-store/src/kv_store/cluster/`
    - `key-value-store/src/kv_store/network/`
    - `key-value-store/proto/`
    - `key-value-store/tests/`
    - `key-value-store/tests/test_storage/`
    - `key-value-store/tests/test_replication/`
    - `key-value-store/tests/test_cluster/`
    - `key-value-store/examples/`
  - 为所有包创建 `__init__.py` 文件

- [x] 1.3 创建 Protobuf 服务定义
  - 文件：`key-value-store/proto/kvstore.proto`
  - 定义 `KVStoreService`，包含 RPC：`Put`, `Get`, `Delete`, `Replicate`, `GossipExchange`, `HintedHandoff`, `MerkleTreeSync`
  - 定义所有消息类型：`PutRequest/Response`, `GetRequest/Response`, `DeleteRequest/Response`, `ReplicateRequest/Response`, `GossipMessage`, `MemberInfoProto`, `HintedHandoffRequest/Response`, `MerkleTreeSyncRequest/Response`, `VectorClockProto`, `VectorClockEntry`, `ConflictingValue`, `KeyValuePair`

- [x] 1.4 创建配置模块
  - 文件：`key-value-store/src/kv_store/config.py`
  - 数据类：`StorageConfig`, `ReplicationConfig`, `ClusterConfig`, `NetworkConfig`, `NodeConfig`
  - 所有字段带设计中指定的合理默认值

- [x] 1.5 验证项目可正确安装
  - 成功运行 `pip install -e ".[dev]"`
  - 运行 `pytest --collect-only` 验证测试发现

---

## Task 2：存储层

- [x] 2.1 实现 Bloom Filter
  - 文件：`key-value-store/src/kv_store/storage/bloom_filter.py`
  - 最优位数组大小：`m = -n * ln(p) / (ln(2))^2`
  - 最优哈希函数数量：`k = (m / n) * ln(2)`
  - 方法：`add(key)`, `might_contain(key)`, `serialize()`, `deserialize(data)`
  - 使用双重哈希（hash1 + i * hash2）实现多个哈希函数
  - 属性：`size_bits`, `num_hash_functions`

- [x] 2.2 编写 Bloom Filter 测试
  - 文件：`key-value-store/tests/test_storage/test_bloom_filter.py`
  - 测试：无误报阴性（添加的键总能被找到）
  - 测试：误报率在预期范围内
  - 测试：序列化/反序列化往返
  - 测试：最优参数计算
  - 测试：空过滤器对所有查询返回 False

- [x] 2.3 实现 Write-Ahead Log（WAL）
  - 文件：`key-value-store/src/kv_store/storage/wal.py`
  - 类：`WALEntryType`（枚举）、`WALEntry`（数据类）、`WriteAheadLog`
  - 二进制格式：`[length][sequence_number][entry_type][key_length][key][value_length][value][timestamp][checksum]`
  - 方法：`append(entry)`, `replay()`, `truncate()`
  - 异步文件 I/O， durability 保证使用 fsync
  - 使用 CRC32 校验和检测损坏

- [x] 2.4 编写 WAL 测试
  - 文件：`key-value-store/tests/test_storage/test_wal.py`
  - 测试：append 与 replay 往返
  - 测试：序列号单调递增
  - 测试：truncate 清空所有条目
  - 测试：优雅处理损坏条目（跳过并发出警告）
  - 测试：并发 append 保持顺序
  - 使用 `tmp_path` fixture 实现隔离测试目录

- [x] 2.5 实现 MemTable
  - 文件：`key-value-store/src/kv_store/storage/memtable.py`
  - 类：`MemTableEntry`（数据类）、`MemTable`
  - 使用 `sortedcontainers.SortedDict` 或手动实现的有序字典，实现 O(log N) 操作
  - 方法：`put(key, value, timestamp)`, `delete(key, timestamp)`, `get(key)`, `is_full()`, `entries_sorted()`, `clear()`
  - 跟踪近似字节数
  - 支持 Tombstone 用于删除操作

- [x] 2.6 编写 MemTable 测试
  - 文件：`key-value-store/tests/test_storage/test_memtable.py`
  - 测试：put 与 get 往返
  - 测试：delete 创建 Tombstone
  - 测试：is_full 在阈值处触发
  - 测试：entries_sorted 按键顺序返回
  - 测试：较新的时间戳覆盖较旧的
  - 测试：大小跟踪大致正确

- [x] 2.7 实现 SSTable
  - 文件：`key-value-store/src/kv_store/storage/sstable.py`
  - 类：`SSTableMetadata`（数据类）、`SSTableEntry`（数据类）、`SSTable`
  - 文件格式：数据块（有序条目）+ 稀疏索引块 + Bloom Filter + 元数据页脚
  - 类方法：`create_from_memtable(memtable, file_path, level)`
  - 方法：`get(key)`, `may_contain(key)`, `entries()`, `metadata`
  - 使用稀疏索引进行二分查找，实现高效查找
  - 集成 Bloom Filter 实现快速否定查找

- [x] 2.8 编写 SSTable 测试
  - 文件：`key-value-store/tests/test_storage/test_sstable.py`
  - 测试：从 memtable 创建并读回所有条目
  - 测试：get 对已存在键返回正确值
  - 测试：get 对不存在键返回 None
  - 测试：may_contain 正确使用 Bloom Filter
  - 测试：entries 迭代器返回有序顺序
  - 测试：元数据正确填充
  - 测试：处理 Tombstone 条目
  - 使用 `tmp_path` fixture

- [x] 2.9 实现 Compaction Manager
  - 文件：`key-value-store/src/kv_store/storage/compaction.py`
  - 类：`CompactionStats`（数据类）、`CompactionManager`
  - 大小分层压缩策略（Size-tiered compaction）
  - K-way 合并有序 SSTable 迭代器
  - 重复条目解决：保留最新时间戳
  - Tombstone 宽限期后移除
  - 方法：`maybe_compact(sstables)`, `compact(sstables, level)`, `get_compaction_stats()`

- [x] 2.10 编写 Compaction 测试
  - 文件：`key-value-store/tests/test_storage/test_compaction.py`
  - 测试：合并键不重叠的两个 SSTable
  - 测试：合并有重复键时保留最新的
  - 测试：Tombstone 在宽限期后被移除
  - 测试：压缩在阈值处触发
  - 测试：输出 SSTable 有序
  - 使用 `tmp_path` fixture

- [x] 2.11 实现 Storage Engine（LSM-Tree 协调器）
  - 文件：`key-value-store/src/kv_store/storage/engine.py`
  - 类：`StorageResult`（数据类）、`StorageEngine`
  - 写入路径：WAL → MemTable →（满时 flush）→ SSTable
  - 读取路径：MemTable → Bloom Filter → SSTable（最新优先）
  - 方法：`start()`, `stop()`, `put(key, value, timestamp)`, `get(key)`, `delete(key, timestamp)`
  - 内部：`_flush_memtable()` —— flush 到 SSTable，截断 WAL，触发压缩检查
  - 键校验：最大 256 字节；值校验：最大 10 KB

- [x] 2.12 编写 Storage Engine 测试
  - 文件：`key-value-store/tests/test_storage/test_engine.py`（或扩展已有文件）
  - 测试：put 与 get 往返
  - 测试：delete 返回 Tombstone
  - 测试：数据在重启后存活（WAL 重放）
  - 测试：memtable flush 创建 SSTable
  - 测试：flush 后在 SSTable 中能找到数据
  - 测试：键/值大小校验
  - 使用 `tmp_path` fixture

- [x] 2.13 创建 storage 包 `__init__.py`，包含公共导出
  - 文件：`key-value-store/src/kv_store/storage/__init__.py`
  - 导出：`StorageEngine`, `StorageConfig`, `BloomFilter`, `WriteAheadLog`, `MemTable`, `SSTable`, `CompactionManager`

---

## Task 3：复制层

- [x] 3.1 实现 Vector Clock
  - 文件：`key-value-store/src/kv_store/replication/vector_clock.py`
  - 类：`VectorClock`
  - 不可变操作，返回新实例
  - 方法：`increment(node_id)`, `merge(other)`, `dominates(other)`, `conflicts_with(other)`, `to_dict()`, `from_dict(data)`
  - 最大条目裁剪：超限时移除最旧（计数器最低）的条目
  - 支持相等性判断和 repr

- [x] 3.2 编写 Vector Clock 测试
  - 文件：`key-value-store/tests/test_replication/test_vector_clock.py`
  - 测试：increment 创建/更新条目
  - 测试：merge 取每个节点计数器的最大值
  - 测试：dominates 检测因果顺序
  - 测试：conflicts_with 检测并发版本
  - 测试：序列化往返（to_dict/from_dict）
  - 测试：最大条目裁剪
  - 测试：空向量时钟互不支配

- [x] 3.3 实现 Quorum 逻辑
  - 文件：`key-value-store/src/kv_store/replication/quorum.py`
  - 类：`ConsistencyLevel`（枚举）、`QuorumConfig`（数据类）、`QuorumResult`（数据类）、`QuorumManager`
  - `QuorumConfig.is_strongly_consistent()`：W + R > N 检查
  - `QuorumConfig.from_consistency_level(level, n)`：工厂方法
  - `QuorumManager.get_quorum_config(consistency)`：解析配置
  - `QuorumManager.write_quorum(...)`：并发写入，等待 W 个确认
  - `QuorumManager.read_quorum(...)`：并发读取，等待 R 个响应
  - 使用 `asyncio.gather` 配合 `return_exceptions=True` 实现并发操作

- [x] 3.4 编写 Quorum 逻辑测试
  - 文件：`key-value-store/tests/test_replication/test_quorum.py`
  - 测试：强一致性检查（W + R > N）
  - 测试：一致性级别工厂方法产生正确的 W/R
  - 测试：写入 Quorum 在 W 个确认时成功
  - 测试：写入 Quorum 在少于 W 个确认时失败
  - 测试：读取 Quorum 收集 R 个响应
  - 测试：读取 Quorum 在少于 R 个响应时失败
  - Mock gRPC 客户端用于网络调用

- [x] 3.5 实现请求协调器（Request Coordinator）
  - 文件：`key-value-store/src/kv_store/replication/coordinator.py`
  - 类：`PutResult`, `GetResult`, `DeleteResult`（数据类）、`RequestCoordinator`
  - 自定义异常：`QuorumNotMetError`
  - `put(key, value, client_clock, consistency)`：确定副本 → 递增时钟 → 本地写入 + 转发 → 等待 W 个确认
  - `get(key, consistency)`：确定副本 → 查询 R 个 → 比较时钟 → 解决或返回冲突
  - `delete(key, client_clock, consistency)`：通过 put 路径写入 Tombstone
  - `_get_replica_nodes(key)`：使用 hash_ring.get_nodes()，对不可用节点应用宽松 Quorum
  - 与 hinted handoff 集成处理失败节点

- [x] 3.6 编写请求协调器测试
  - 文件：`key-value-store/tests/test_replication/test_coordinator.py`
  - 测试：put 写入 N 个副本，在 W 个确认后返回
  - 测试：get 从 R 个副本读取，返回最新值
  - 测试：冲突检测将所有版本返回给客户端
  - 测试：宽松 Quorum 替换不可用节点
  - 测试：对失败节点触发 hinted handoff
  - 测试：delete 写入 Tombstone
  - Mock 存储引擎、gRPC 客户端、成员关系

- [x] 3.7 创建 replication 包 `__init__.py`，包含公共导出
  - 文件：`key-value-store/src/kv_store/replication/__init__.py`
  - 导出：`VectorClock`, `QuorumManager`, `QuorumConfig`, `ConsistencyLevel`, `RequestCoordinator`

---

## Task 4：集群层

- [x] 4.1 实现 Gossip 协议
  - 文件：`key-value-store/src/kv_store/cluster/gossip.py`
  - 类：`NodeStatus`（枚举）、`MemberInfo`（数据类）、`GossipProtocol`
  - 后台任务：以可配置间隔周期性执行 gossip 轮
  - `_gossip_round()`：递增心跳 → 选择随机对等节点（fanout） → 发送成员列表 → 检查超时
  - `merge_membership(remote_members)`：取每个节点的最大心跳
  - `get_alive_nodes()`, `get_node_status(node_id)`, `add_seed_node(node_id, address)`
  - 故障检测：超时后标记为 SUSPECTED，2 倍超时后标记为 DOWN

- [x] 4.2 编写 Gossip 协议测试
  - 文件：`key-value-store/tests/test_cluster/test_gossip.py`
  - 测试：心跳每轮递增
  - 测试：merge 取较高的心跳计数器
  - 测试：节点在超时后标记为可疑
  - 测试：节点在延长超时后标记为下线
  - 测试：新节点通过 merge 添加
  - 测试：gossip fanout 选择正确数量的对等节点
  - Mock gRPC 客户端，使用虚拟定时器

- [x] 4.3 实现集群成员关系
  - 文件：`key-value-store/src/kv_store/cluster/membership.py`
  - 类：`ClusterMembership`
  - 封装 gossip 协议 + 一致性哈希环
  - `join_cluster(seed_nodes)`：将自身加入环 → 联系种子节点 → 启动 gossip
  - `leave_cluster()`：通知对等节点 → 从环移除 → 停止 gossip
  - `is_node_alive(node_id)`, `get_node_address(node_id)`, `get_alive_members()`
  - 事件处理器：`on_node_joined(node_id, address)`, `on_node_failed(node_id)`
  - 成员关系变化时更新哈希环

- [x] 4.4 编写集群成员关系测试
  - 文件：`key-value-store/tests/test_cluster/test_membership.py`
  - 测试：join 将节点加入哈希环
  - 测试：leave 将节点从哈希环移除
  - 测试：节点故障时从环中移除
  - 测试：get_alive_members 仅返回存活节点
  - 测试：get_node_address 返回正确地址
  - Mock gossip 协议和哈希环

- [x] 4.5 实现 Hinted Handoff 管理器
  - 文件：`key-value-store/src/kv_store/cluster/hinted_handoff.py`
  - 类：`HintedData`（数据类）、`HintedHandoffManager`
  - 内存中提示存储（以 target_node_id 为键的字典）
  - `store_hint(hint)`：本地存储
  - `deliver_hints(target_node_id)`：通过 gRPC 将所有提示发送给恢复的节点
  - `_handoff_loop()`：后台任务，检查恢复的节点
  - `get_pending_hints(target_node_id)`, `pending_count`
  - 成功交付后删除提示

- [x] 4.6 编写 Hinted Handoff 测试
  - 文件：`key-value-store/tests/test_cluster/test_hinted_handoff.py`
  - 测试：存储和检索提示
  - 测试：将提示交付给恢复的节点
  - 测试：成功交付后提示被删除
  - 测试：pending count 正确跟踪
  - 测试：handoff 循环对存活节点尝试交付
  - Mock gRPC 客户端和成员关系

- [x] 4.7 实现 Merkle Tree 和反熵管理器
  - 文件：`key-value-store/src/kv_store/cluster/merkle_tree.py`
  - 类：`MerkleNode`（数据类）、`SyncDiff`（数据类）、`MerkleTree`, `AntiEntropyManager`
  - `MerkleTree`：基于桶，桶数量可配置（2 的幂）
  - `update(key, value_hash)`：更新叶节点 → 重新计算到根的路径
  - `remove(key)`：从桶中移除 → 重新计算
  - `get_root_hash()`, `get_bucket_hash(bucket_id)`, `compare(other_tree_hashes)`
  - `_key_to_bucket(key)`：将键哈希到桶索引
  - `AntiEntropyManager`：周期性地与副本对等节点同步
  - `sync_with_peer(peer_node_id)`：交换哈希 → 发现差异 → 同步数据

- [x] 4.8 编写 Merkle Tree 测试
  - 文件：`key-value-store/tests/test_cluster/test_merkle_tree.py`
  - 测试：相同的树具有相同的根哈希
  - 测试：不同数据产生不同根哈希
  - 测试：compare 发现有差异的桶
  - 测试：update 仅改变受影响的路径
  - 测试：remove key 改变哈希
  - 测试：get_keys_in_bucket 返回正确的键
  - 测试：key_to_bucket 具有确定性

- [x] 4.9 创建 cluster 包 `__init__.py`，包含公共导出
  - 文件：`key-value-store/src/kv_store/cluster/__init__.py`
  - 导出：`GossipProtocol`, `NodeStatus`, `MemberInfo`, `ClusterMembership`, `HintedHandoffManager`, `MerkleTree`, `AntiEntropyManager`

---

## Task 5：网络层

- [x] 5.1 编译 Protobuf 并生成 Python gRPC 存根
  - 文件：`key-value-store/src/kv_store/network/__init__.py`
  - 添加构建脚本或 Makefile 目标：`python -m grpc_tools.protoc -I proto --python_out=src/kv_store/network --grpc_python_out=src/kv_store/network proto/kvstore.proto`
  - 生成文件：`kvstore_pb2.py`, `kvstore_pb2_grpc.py`
  - 添加 `py.typed` 标记以支持类型检查
  - 在 README 或 pyproject.toml 脚本中记录重新生成命令

- [x] 5.2 实现 gRPC 服务器
  - 文件：`key-value-store/src/kv_store/network/grpc_server.py`
  - 类：`GRPCServer`，实现 `KVStoreServiceServicer`
  - RPC 处理器：`Put`, `Get`, `Delete` → 委托给 `RequestCoordinator`
  - RPC 处理器：`Replicate` → 直接写入本地 `StorageEngine`
  - RPC 处理器：`GossipExchange` → 通过 `GossipProtocol` 合并成员关系
  - RPC 处理器：`HintedHandoff` → 写入本地存储
  - RPC 处理器：`MerkleTreeSync` → 比较树并返回差异
  - 使用 `grpc.aio.server()` 的异步服务器
  - 可配置 host、port、最大消息大小

- [x] 5.3 实现 gRPC 客户端
  - 文件：`key-value-store/src/kv_store/network/grpc_client.py`
  - 类：`GRPCClient`
  - 连接池：为每个目标地址维护 channel
  - 方法：`put(target, key, value, vector_clock)`, `get(target, key)`, `replicate(target, ...)`, `gossip_exchange(target, members)`, `send_hinted_handoff(target, hint)`, `merkle_tree_sync(target, bucket_hashes)`
  - 自定义异常：`NodeUnavailableError`
  - 优雅处理连接失败（超时、不可用）
  - `close()`：关闭所有 channel

- [x] 5.4 编写网络层测试
  - 文件：`key-value-store/tests/test_network/`（创建目录 + `__init__.py`）
  - 测试：gRPC 服务器干净启动和停止
  - 测试：Put/Get/Delete RPC 端到端工作（进程内）
  - 测试：Replicate RPC 写入本地存储
  - 测试：GossipExchange RPC 合并成员关系
  - 测试：客户端优雅处理不可用节点
  - 测试：连接池复用 channel
  - 使用 `grpcio-testing` 或进程内服务器进行测试

- [x] 5.5 创建 network 包 `__init__.py`，包含公共导出
  - 文件：`key-value-store/src/kv_store/network/__init__.py`
  - 导出：`GRPCServer`, `GRPCClient`, `NodeUnavailableError`

---

## Task 6：节点协调器

- [x] 6.1 实现 KVNode 协调器
  - 文件：`key-value-store/src/kv_store/node.py`
  - 类：`KVNode`
  - 通过依赖注入初始化所有子组件
  - `start()`：存储引擎 → gRPC 服务器 → 加入集群 → 启动后台任务
  - `stop()`：停止后台任务 → 离开集群 → 停止 gRPC → flush 存储
  - 委托 `put/get/delete` 给 `RequestCoordinator`
  - 使用 `consistent-hashing` 库的 `ConsistentHashRing`

- [x] 6.2 创建顶层包 `__init__.py`
  - 文件：`key-value-store/src/kv_store/__init__.py`
  - 导出：`KVNode`, `NodeConfig`, `StorageConfig`, `ReplicationConfig`, `ClusterConfig`, `NetworkConfig`
  - 版本：`__version__ = "0.1.0"`

- [x] 6.3 编写节点协调器测试
  - 文件：`key-value-store/tests/test_node.py`
  - 测试：节点按正确顺序启动所有组件
  - 测试：节点按正确顺序停止所有组件
  - 测试：put/get/delete 委托给 coordinator
  - 测试：节点优雅处理启动失败
  - Mock 所有子组件

---

## Task 7：客户端 API

- [x] 7.1 实现 KVClient
  - 文件：`key-value-store/src/kv_store/client.py`
  - 类：`ClientConfig`（数据类）、`KVResponse`（数据类）、`KVClient`
  - `connect()`：与种子节点建立 gRPC channel
  - `put(key, value, consistency)`：发送 PutRequest，返回包含向量时钟的 KVResponse
  - `get(key, consistency)`：发送 GetRequest，处理冲突
  - `delete(key, consistency)`：发送 DeleteRequest
  - `close()`：关闭 gRPC channel
  - 带可配置次数和延迟的重试逻辑
  - 连接失败时自动故障转移到下一个种子节点
  - 上下文管理器支持（`async with KVClient(...) as client:`）

- [x] 7.2 编写客户端 API 测试
  - 文件：`key-value-store/tests/test_client.py`
  - 测试：通过 gRPC 的 put/get/delete 往返
  - 测试：临时失败时重试
  - 测试：故障转移到下一个种子节点
  - 测试：上下文管理器打开和关闭连接
  - 测试：超时处理
  - 使用进程内 gRPC 服务器或 mocks

---

## Task 8：集成测试与基于属性的测试

- [x] 8.1 编写单节点集成测试
  - 文件：`key-value-store/tests/test_integration.py`
  - 测试：单节点上完整的 put/get/delete 生命周期
  - 测试：数据在节点重启后持久化
  - 测试：memtable flush 和 SSTable 读取路径
  - 测试：大量键（压力测试）
  - 测试：通过 asyncio 的并发操作

- [x] 8.2 编写多节点集成测试
  - 文件：`key-value-store/tests/test_multi_node.py`
  - 测试：通过 Gossip 形成 3 节点集群
  - 测试：在一个节点上 put，在另一个节点上 get（复制）
  - 测试：节点故障和恢复配合 hinted handoff
  - 测试：一个节点下线时的 Quorum 写入/读取
  - 测试：宽松 Quorum 路由到替代节点
  - 启动多个使用不同端口的进程内节点

- [x] 8.3 使用 Hypothesis 编写基于属性的测试
  - 文件：`key-value-store/tests/test_properties.py`
  - **Vector Clock 属性：**
    - 属性：increment 总是产生支配性的时钟
    - 属性：merge 满足交换律（merge(a,b) == merge(b,a)）
    - 属性：merge 满足幂等性（merge(a,a) == a）
    - 属性：若 a 支配 b，则 b 不支配 a
    - 属性：conflicts_with 是对称的
  - **Bloom Filter 属性：**
    - 属性：无误报阴性（添加的条目总能被找到）
    - 属性：误报率在预期率的 2 倍以内（统计意义）
  - **MemTable 属性：**
    - 属性：put 后 get 返回 put 的值
    - 属性：entries_sorted 总是按键有序
    - 属性：delete 创建可检索的 Tombstone
  - **Storage Engine 属性：**
    - 属性：put 然后 get 返回相同值（往返）
    - 属性：delete 然后 get 返回 Tombstone 或 not found
  - **Quorum 属性：**
    - 属性：W + R > N 意味着强一致性
    - 属性：写入成功当且仅当接收到 >= W 个确认
  - 使用 Hypothesis strategies 生成随机键、值和操作

- [x] 8.4 编写 WAL 崩溃恢复测试
  - 文件：`key-value-store/tests/test_storage/test_wal_recovery.py`
  - 测试：部分写入（模拟 append 中途崩溃）被检测并跳过
  - 测试：正常关闭后重放恢复所有条目
  - 测试：崩溃后重放恢复所有完整条目
  - 测试：flush 后截断意味着下次启动时无需重放

---

## Task 9：示例与文档

- [-] 9.1 创建演示脚本
  - 文件：`key-value-store/examples/demo.py`
  - 演示：启动一个 3 节点集群
  - 演示：通过客户端进行 put/get/delete 操作
  - 演示：节点故障和恢复
  - 演示：通过并发写入检测冲突
  - 包含解释每个步骤的注释
  - 可通过 `python examples/demo.py` 运行

- [~] 9.2 创建 README.md
  - 文件：`key-value-store/README.md`
  - 项目概述和架构图
  - 安装说明（`pip install -e ".[dev]"`）
  - 快速入门指南（单节点和多节点）
  - 配置参考（所有配置数据类）
  - API 参考（KVClient 方法）
  - 设计决策与权衡
  - Protobuf 重新生成说明

- [~] 9.3 移动/更新文档文件
  - 文件：`key-value-store/docs/design.md` —— 从 `.kiro/specs/key-value-store/design.md` 复制
  - 文件：`key-value-store/docs/requirements.md` —— 已存在，验证是否最新
  - 文件：`key-value-store/docs/tasks.md` —— 从 `.kiro/specs/key-value-store/tasks.md` 复制

- [~] 9.4 添加内联代码文档
  - 所有公共类和方法带有 docstring
  - 模块级 docstring 解释用途
  - 所有函数签名带类型注解
  - 非显而易见的算法带注释（Bloom Filter 哈希、K-way 合并、向量时钟比较）


## 2026-10-27本轮复审

- [x] 对照原书1735–2213行与图依赖，明确覆盖/尚未完成范围。
- [x] 增量Merkle根、Inspect分层协议、差异桶排序游标/字节分页、root变更拒绝。
- [x] 输入冻结/严格记录协议、提交取消排空、单调物理记录时间及跨层读取。
- [x] SSTable fsync/原子发布/压缩输入退役、关闭失败排空/FD关闭、损坏表拒绝。
- [x] hint迟到ACK精确记录匹配，真实SQLite ID复用竞争验证。
- [x] 417项测试、真实gRPC修复示例、原组件演示及基线/本轮原始性能记录。
- [x] 双语README/设计/需求、本章覆盖与兼容性、性能回归同步更新。
- [ ] 负载驱动资源供给/安全缩容屏障、拓扑共识与真正跨机房运行保障。
- [ ] 流式压缩/有界SSTable缓存/海量数据恢复、共有职责树分片与持久修复续传。
