# Tasks: Distributed Key-Value Store

## Task Dependency Graph

```
Task 1 (Project Scaffolding)
  │
  ├──► Task 2 (Storage Layer)
  │       │
  ├──► Task 3 (Replication Layer)
  │       │
  ├──► Task 4 (Cluster Layer)
  │       │
  ├──► Task 5 (Network Layer)
  │       │       │
  │       ▼       ▼
  │     Task 6 (Node Orchestrator) ◄── Task 2, 3, 4, 5
  │       │
  │       ▼
  │     Task 7 (Client API) ◄── Task 5, 6
  │       │
  │       ▼
  │     Task 8 (Integration & Property-Based Tests) ◄── Task 2–7
  │       │
  │       ▼
  └───► Task 9 (Examples & Documentation) ◄── Task 7, 8
```

**Dependencies:**
- Task 1: No dependencies (foundation)
- Task 2: Depends on Task 1
- Task 3: Depends on Task 1
- Task 4: Depends on Task 1
- Task 5: Depends on Task 1
- Task 6: Depends on Tasks 2, 3, 4, 5
- Task 7: Depends on Tasks 5, 6
- Task 8: Depends on Tasks 2, 3, 4, 5, 6, 7
- Task 9: Depends on Tasks 7, 8

---

## Task 1: Project Scaffolding

- [x] 1.1 Create `pyproject.toml` with hatchling build system
  - File: `key-value-store/pyproject.toml`
  - Python >= 3.9
  - Dependencies: `grpcio`, `grpcio-tools`, `protobuf`, `consistent-hashing` (local path: `../consistent-hashing`)
  - Dev dependencies: `pytest`, `pytest-asyncio`, `hypothesis`, `grpcio-testing`
  - Build target: `packages = ["src/kv_store"]`
  - Pytest config: `testpaths = ["tests"]`, `pythonpath = ["src"]`, `asyncio_mode = "auto"`

- [x] 1.2 Create directory structure
  - Directories:
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
  - Create `__init__.py` files for all packages

- [x] 1.3 Create protobuf service definition
  - File: `key-value-store/proto/kvstore.proto`
  - Define `KVStoreService` with RPCs: `Put`, `Get`, `Delete`, `Replicate`, `GossipExchange`, `HintedHandoff`, `MerkleTreeSync`
  - Define all message types: `PutRequest/Response`, `GetRequest/Response`, `DeleteRequest/Response`, `ReplicateRequest/Response`, `GossipMessage`, `MemberInfoProto`, `HintedHandoffRequest/Response`, `MerkleTreeSyncRequest/Response`, `VectorClockProto`, `VectorClockEntry`, `ConflictingValue`, `KeyValuePair`

- [x] 1.4 Create configuration module
  - File: `key-value-store/src/kv_store/config.py`
  - Dataclasses: `StorageConfig`, `ReplicationConfig`, `ClusterConfig`, `NetworkConfig`, `NodeConfig`
  - All fields with sensible defaults as specified in design

- [x] 1.5 Verify project installs correctly
  - Run `pip install -e ".[dev]"` successfully
  - Run `pytest --collect-only` to verify test discovery

---

## Task 2: Storage Layer

- [x] 2.1 Implement Bloom Filter
  - File: `key-value-store/src/kv_store/storage/bloom_filter.py`
  - Optimal bit array size: `m = -n * ln(p) / (ln(2))^2`
  - Optimal hash count: `k = (m / n) * ln(2)`
  - Methods: `add(key)`, `might_contain(key)`, `serialize()`, `deserialize(data)`
  - Use multiple hash functions via double hashing (hash1 + i * hash2)
  - Properties: `size_bits`, `num_hash_functions`

- [x] 2.2 Write Bloom Filter tests
  - File: `key-value-store/tests/test_storage/test_bloom_filter.py`
  - Test: no false negatives (added keys always found)
  - Test: false positive rate within expected bounds
  - Test: serialization/deserialization roundtrip
  - Test: optimal parameter calculation
  - Test: empty filter returns False for all queries

- [x] 2.3 Implement Write-Ahead Log (WAL)
  - File: `key-value-store/src/kv_store/storage/wal.py`
  - Classes: `WALEntryType` (enum), `WALEntry` (dataclass), `WriteAheadLog`
  - Binary format: `[length][sequence_number][entry_type][key_length][key][value_length][value][timestamp][checksum]`
  - Methods: `append(entry)`, `replay()`, `truncate()`
  - Async file I/O with fsync for durability
  - CRC32 checksum for corruption detection

- [x] 2.4 Write WAL tests
  - File: `key-value-store/tests/test_storage/test_wal.py`
  - Test: append and replay roundtrip
  - Test: sequence numbers are monotonically increasing
  - Test: truncate clears all entries
  - Test: handles corrupt entries gracefully (skip with warning)
  - Test: concurrent appends maintain ordering
  - Use `tmp_path` fixture for isolated test directories

- [x] 2.5 Implement MemTable
  - File: `key-value-store/src/kv_store/storage/memtable.py`
  - Classes: `MemTableEntry` (dataclass), `MemTable`
  - Use `sortedcontainers.SortedDict` or manual sorted dict for O(log N) operations
  - Methods: `put(key, value, timestamp)`, `delete(key, timestamp)`, `get(key)`, `is_full()`, `entries_sorted()`, `clear()`
  - Track approximate size in bytes
  - Tombstone support for deletes

- [x] 2.6 Write MemTable tests
  - File: `key-value-store/tests/test_storage/test_memtable.py`
  - Test: put and get roundtrip
  - Test: delete creates tombstone
  - Test: is_full triggers at threshold
  - Test: entries_sorted returns keys in order
  - Test: newer timestamp overwrites older
  - Test: size tracking is approximately correct

- [x] 2.7 Implement SSTable
  - File: `key-value-store/src/kv_store/storage/sstable.py`
  - Classes: `SSTableMetadata` (dataclass), `SSTableEntry` (dataclass), `SSTable`
  - File format: data block (sorted entries) + sparse index block + bloom filter + metadata footer
  - Class method: `create_from_memtable(memtable, file_path, level)`
  - Methods: `get(key)`, `may_contain(key)`, `entries()`, `metadata`
  - Binary search using sparse index for efficient lookups
  - Integrate Bloom filter for fast negative lookups

- [x] 2.8 Write SSTable tests
  - File: `key-value-store/tests/test_storage/test_sstable.py`
  - Test: create from memtable and read back all entries
  - Test: get returns correct value for existing key
  - Test: get returns None for non-existent key
  - Test: may_contain uses bloom filter correctly
  - Test: entries iterator returns sorted order
  - Test: metadata is populated correctly
  - Test: handles tombstone entries
  - Use `tmp_path` fixture

- [x] 2.9 Implement Compaction Manager
  - File: `key-value-store/src/kv_store/storage/compaction.py`
  - Classes: `CompactionStats` (dataclass), `CompactionManager`
  - Size-tiered compaction strategy
  - K-way merge of sorted SSTable iterators
  - Duplicate resolution: keep newest timestamp
  - Tombstone removal after grace period
  - Methods: `maybe_compact(sstables)`, `compact(sstables, level)`, `get_compaction_stats()`

- [x] 2.10 Write Compaction tests
  - File: `key-value-store/tests/test_storage/test_compaction.py`
  - Test: merge two SSTables with non-overlapping keys
  - Test: merge with duplicate keys keeps newest
  - Test: tombstones are removed after grace period
  - Test: compaction triggers at threshold
  - Test: output SSTable is sorted
  - Use `tmp_path` fixture

- [x] 2.11 Implement Storage Engine (LSM-tree orchestrator)
  - File: `key-value-store/src/kv_store/storage/engine.py`
  - Classes: `StorageResult` (dataclass), `StorageEngine`
  - Write path: WAL → MemTable → (flush when full) → SSTable
  - Read path: MemTable → Bloom filters → SSTables (newest first)
  - Methods: `start()`, `stop()`, `put(key, value, timestamp)`, `get(key)`, `delete(key, timestamp)`
  - Internal: `_flush_memtable()` — flush to SSTable, truncate WAL, trigger compaction check
  - Key validation: max 256 bytes; value validation: max 10 KB

- [x] 2.12 Write Storage Engine tests
  - File: `key-value-store/tests/test_storage/test_engine.py` (or extend existing)
  - Test: put and get roundtrip
  - Test: delete returns tombstone
  - Test: data survives restart (WAL replay)
  - Test: memtable flush creates SSTable
  - Test: read after flush finds data in SSTable
  - Test: key/value size validation
  - Use `tmp_path` fixture

- [x] 2.13 Create storage package `__init__.py` with public exports
  - File: `key-value-store/src/kv_store/storage/__init__.py`
  - Export: `StorageEngine`, `StorageConfig`, `BloomFilter`, `WriteAheadLog`, `MemTable`, `SSTable`, `CompactionManager`

---

## Task 3: Replication Layer

- [x] 3.1 Implement Vector Clock
  - File: `key-value-store/src/kv_store/replication/vector_clock.py`
  - Classes: `VectorClock`
  - Immutable operations returning new instances
  - Methods: `increment(node_id)`, `merge(other)`, `dominates(other)`, `conflicts_with(other)`, `to_dict()`, `from_dict(data)`
  - Max entries pruning: remove oldest (lowest counter) when exceeded
  - Equality and repr support

- [x] 3.2 Write Vector Clock tests
  - File: `key-value-store/tests/test_replication/test_vector_clock.py`
  - Test: increment creates/updates entry
  - Test: merge takes max of each node's counter
  - Test: dominates detects causal ordering
  - Test: conflicts_with detects concurrent versions
  - Test: serialization roundtrip (to_dict/from_dict)
  - Test: max entries pruning
  - Test: empty clocks don't dominate each other

- [x] 3.3 Implement Quorum Logic
  - File: `key-value-store/src/kv_store/replication/quorum.py`
  - Classes: `ConsistencyLevel` (enum), `QuorumConfig` (dataclass), `QuorumResult` (dataclass), `QuorumManager`
  - `QuorumConfig.is_strongly_consistent()`: W + R > N check
  - `QuorumConfig.from_consistency_level(level, n)`: factory method
  - `QuorumManager.get_quorum_config(consistency)`: resolve config
  - `QuorumManager.write_quorum(...)`: concurrent writes, wait for W acks
  - `QuorumManager.read_quorum(...)`: concurrent reads, wait for R responses
  - Use `asyncio.gather` with `return_exceptions=True` for concurrent operations

- [x] 3.4 Write Quorum Logic tests
  - File: `key-value-store/tests/test_replication/test_quorum.py`
  - Test: strong consistency check (W + R > N)
  - Test: consistency level factory produces correct W/R
  - Test: write quorum succeeds with W acks
  - Test: write quorum fails with fewer than W acks
  - Test: read quorum collects R responses
  - Test: read quorum fails with fewer than R responses
  - Mock gRPC client for network calls

- [x] 3.5 Implement Request Coordinator
  - File: `key-value-store/src/kv_store/replication/coordinator.py`
  - Classes: `PutResult`, `GetResult`, `DeleteResult` (dataclasses), `RequestCoordinator`
  - Custom exceptions: `QuorumNotMetError`
  - `put(key, value, client_clock, consistency)`: determine replicas → increment clock → write locally + forward → wait for W
  - `get(key, consistency)`: determine replicas → query R → compare clocks → resolve or return conflicts
  - `delete(key, client_clock, consistency)`: tombstone write via put path
  - `_get_replica_nodes(key)`: use hash_ring.get_nodes(), apply sloppy quorum for unavailable nodes
  - Integrate with hinted handoff for failed nodes

- [x] 3.6 Write Request Coordinator tests
  - File: `key-value-store/tests/test_replication/test_coordinator.py`
  - Test: put writes to N replicas and returns after W acks
  - Test: get reads from R replicas and returns newest value
  - Test: conflict detection returns all versions to client
  - Test: sloppy quorum substitutes unavailable nodes
  - Test: hinted handoff triggered for failed nodes
  - Test: delete writes tombstone
  - Mock storage engine, gRPC client, membership

- [x] 3.7 Create replication package `__init__.py` with public exports
  - File: `key-value-store/src/kv_store/replication/__init__.py`
  - Export: `VectorClock`, `QuorumManager`, `QuorumConfig`, `ConsistencyLevel`, `RequestCoordinator`

---

## Task 4: Cluster Layer

- [x] 4.1 Implement Gossip Protocol
  - File: `key-value-store/src/kv_store/cluster/gossip.py`
  - Classes: `NodeStatus` (enum), `MemberInfo` (dataclass), `GossipProtocol`
  - Background task: periodic gossip rounds at configurable interval
  - `_gossip_round()`: increment heartbeat → select random peers (fanout) → send membership → check timeouts
  - `merge_membership(remote_members)`: take max heartbeat per node
  - `get_alive_nodes()`, `get_node_status(node_id)`, `add_seed_node(node_id, address)`
  - Failure detection: mark as SUSPECTED after timeout, DOWN after 2x timeout

- [x] 4.2 Write Gossip Protocol tests
  - File: `key-value-store/tests/test_cluster/test_gossip.py`
  - Test: heartbeat increments each round
  - Test: merge takes higher heartbeat counter
  - Test: node marked suspected after timeout
  - Test: node marked down after extended timeout
  - Test: new nodes added via merge
  - Test: gossip fanout selects correct number of peers
  - Mock gRPC client, use fake timers

- [x] 4.3 Implement Cluster Membership
  - File: `key-value-store/src/kv_store/cluster/membership.py`
  - Classes: `ClusterMembership`
  - Wraps gossip protocol + consistent hash ring
  - `join_cluster(seed_nodes)`: add self to ring → contact seeds → start gossip
  - `leave_cluster()`: notify peers → remove from ring → stop gossip
  - `is_node_alive(node_id)`, `get_node_address(node_id)`, `get_alive_members()`
  - Event handlers: `on_node_joined(node_id, address)`, `on_node_failed(node_id)`
  - Update hash ring on membership changes

- [x] 4.4 Write Cluster Membership tests
  - File: `key-value-store/tests/test_cluster/test_membership.py`
  - Test: join adds node to hash ring
  - Test: leave removes node from hash ring
  - Test: node failure removes from ring
  - Test: get_alive_members returns only alive nodes
  - Test: get_node_address returns correct address
  - Mock gossip protocol and hash ring

- [x] 4.5 Implement Hinted Handoff Manager
  - File: `key-value-store/src/kv_store/cluster/hinted_handoff.py`
  - Classes: `HintedData` (dataclass), `HintedHandoffManager`
  - In-memory hint storage (dict keyed by target_node_id)
  - `store_hint(hint)`: store locally
  - `deliver_hints(target_node_id)`: send all hints for recovered node via gRPC
  - `_handoff_loop()`: background task checking for recovered nodes
  - `get_pending_hints(target_node_id)`, `pending_count`
  - Delete hints after successful delivery

- [x] 4.6 Write Hinted Handoff tests
  - File: `key-value-store/tests/test_cluster/test_hinted_handoff.py`
  - Test: store and retrieve hints
  - Test: deliver hints to recovered node
  - Test: hints deleted after successful delivery
  - Test: pending count tracks correctly
  - Test: handoff loop attempts delivery for alive nodes
  - Mock gRPC client and membership

- [x] 4.7 Implement Merkle Tree and Anti-Entropy Manager
  - File: `key-value-store/src/kv_store/cluster/merkle_tree.py`
  - Classes: `MerkleNode` (dataclass), `SyncDiff` (dataclass), `MerkleTree`, `AntiEntropyManager`
  - `MerkleTree`: bucket-based with configurable bucket count (power of 2)
  - `update(key, value_hash)`: update leaf → recompute path to root
  - `remove(key)`: remove from bucket → recompute
  - `get_root_hash()`, `get_bucket_hash(bucket_id)`, `compare(other_tree_hashes)`
  - `_key_to_bucket(key)`: hash key to bucket index
  - `AntiEntropyManager`: periodic sync with replica peers
  - `sync_with_peer(peer_node_id)`: exchange hashes → find diffs → sync data

- [x] 4.8 Write Merkle Tree tests
  - File: `key-value-store/tests/test_cluster/test_merkle_tree.py`
  - Test: identical trees have same root hash
  - Test: different data produces different root hash
  - Test: compare finds differing buckets
  - Test: update changes only affected path
  - Test: remove key changes hash
  - Test: get_keys_in_bucket returns correct keys
  - Test: key_to_bucket is deterministic

- [x] 4.9 Create cluster package `__init__.py` with public exports
  - File: `key-value-store/src/kv_store/cluster/__init__.py`
  - Export: `GossipProtocol`, `NodeStatus`, `MemberInfo`, `ClusterMembership`, `HintedHandoffManager`, `MerkleTree`, `AntiEntropyManager`

---

## Task 5: Network Layer

- [x] 5.1 Compile protobuf and generate Python gRPC stubs
  - File: `key-value-store/src/kv_store/network/__init__.py`
  - Add build script or Makefile target: `python -m grpc_tools.protoc -I proto --python_out=src/kv_store/network --grpc_python_out=src/kv_store/network proto/kvstore.proto`
  - Generated files: `kvstore_pb2.py`, `kvstore_pb2_grpc.py`
  - Add `py.typed` marker for type checking support
  - Document regeneration command in README or pyproject.toml scripts

- [x] 5.2 Implement gRPC Server
  - File: `key-value-store/src/kv_store/network/grpc_server.py`
  - Class: `GRPCServer` implementing `KVStoreServiceServicer`
  - RPC handlers: `Put`, `Get`, `Delete` → delegate to `RequestCoordinator`
  - RPC handlers: `Replicate` → write directly to local `StorageEngine`
  - RPC handlers: `GossipExchange` → merge membership via `GossipProtocol`
  - RPC handlers: `HintedHandoff` → write to local storage
  - RPC handlers: `MerkleTreeSync` → compare trees and return diffs
  - Async server using `grpc.aio.server()`
  - Configurable host, port, max message size

- [x] 5.3 Implement gRPC Client
  - File: `key-value-store/src/kv_store/network/grpc_client.py`
  - Class: `GRPCClient`
  - Connection pooling: maintain channels per target address
  - Methods: `put(target, key, value, vector_clock)`, `get(target, key)`, `replicate(target, ...)`, `gossip_exchange(target, members)`, `send_hinted_handoff(target, hint)`, `merkle_tree_sync(target, bucket_hashes)`
  - Custom exception: `NodeUnavailableError`
  - Graceful handling of connection failures (timeout, unavailable)
  - `close()`: close all channels

- [x] 5.4 Write Network Layer tests
  - File: `key-value-store/tests/test_network/` (create directory + `__init__.py`)
  - Test: gRPC server starts and stops cleanly
  - Test: Put/Get/Delete RPCs work end-to-end (in-process)
  - Test: Replicate RPC writes to local storage
  - Test: GossipExchange RPC merges membership
  - Test: client handles unavailable node gracefully
  - Test: connection pooling reuses channels
  - Use `grpcio-testing` or in-process server for tests

- [x] 5.5 Create network package `__init__.py` with public exports
  - File: `key-value-store/src/kv_store/network/__init__.py`
  - Export: `GRPCServer`, `GRPCClient`, `NodeUnavailableError`

---

## Task 6: Node Orchestrator

- [x] 6.1 Implement KVNode orchestrator
  - File: `key-value-store/src/kv_store/node.py`
  - Class: `KVNode`
  - Initialize all sub-components with proper dependency injection
  - `start()`: storage engine → gRPC server → join cluster → start background tasks
  - `stop()`: stop background tasks → leave cluster → stop gRPC → flush storage
  - Delegate `put/get/delete` to `RequestCoordinator`
  - Use `ConsistentHashRing` from `consistent-hashing` library

- [x] 6.2 Create top-level package `__init__.py`
  - File: `key-value-store/src/kv_store/__init__.py`
  - Export: `KVNode`, `NodeConfig`, `StorageConfig`, `ReplicationConfig`, `ClusterConfig`, `NetworkConfig`
  - Version: `__version__ = "0.1.0"`

- [x] 6.3 Write Node Orchestrator tests
  - File: `key-value-store/tests/test_node.py`
  - Test: node starts all components in correct order
  - Test: node stops all components in correct order
  - Test: put/get/delete delegate to coordinator
  - Test: node handles startup failure gracefully
  - Mock all sub-components

---

## Task 7: Client API

- [x] 7.1 Implement KVClient
  - File: `key-value-store/src/kv_store/client.py`
  - Classes: `ClientConfig` (dataclass), `KVResponse` (dataclass), `KVClient`
  - `connect()`: establish gRPC channel to a seed node
  - `put(key, value, consistency)`: send PutRequest, return KVResponse with vector clock
  - `get(key, consistency)`: send GetRequest, handle conflicts
  - `delete(key, consistency)`: send DeleteRequest
  - `close()`: close gRPC channel
  - Retry logic with configurable count and delay
  - Automatic failover to next seed node on connection failure
  - Context manager support (`async with KVClient(...) as client:`)

- [x] 7.2 Write Client API tests
  - File: `key-value-store/tests/test_client.py`
  - Test: put/get/delete roundtrip via gRPC
  - Test: retry on transient failure
  - Test: failover to next seed node
  - Test: context manager opens and closes connection
  - Test: timeout handling
  - Use in-process gRPC server or mocks

---

## Task 8: Integration Tests and Property-Based Tests

- [x] 8.1 Write single-node integration tests
  - File: `key-value-store/tests/test_integration.py`
  - Test: full put/get/delete lifecycle on single node
  - Test: data persists across node restart
  - Test: memtable flush and SSTable read path
  - Test: large number of keys (stress test)
  - Test: concurrent operations via asyncio

- [x] 8.2 Write multi-node integration tests
  - File: `key-value-store/tests/test_multi_node.py`
  - Test: 3-node cluster formation via gossip
  - Test: put on one node, get from another (replication)
  - Test: node failure and recovery with hinted handoff
  - Test: quorum write/read with one node down
  - Test: sloppy quorum routes to substitute node
  - Spin up multiple in-process nodes with different ports

- [x] 8.3 Write property-based tests with Hypothesis
  - File: `key-value-store/tests/test_properties.py`
  - **Vector Clock properties:**
    - Property: increment always produces a dominating clock
    - Property: merge is commutative (merge(a,b) == merge(b,a))
    - Property: merge is idempotent (merge(a,a) == a)
    - Property: if a dominates b, then b does not dominate a
    - Property: conflicts_with is symmetric
  - **Bloom Filter properties:**
    - Property: no false negatives (added items always found)
    - Property: false positive rate within 2x expected rate (statistical)
  - **MemTable properties:**
    - Property: get after put returns the put value
    - Property: entries_sorted is always in sorted key order
    - Property: delete creates retrievable tombstone
  - **Storage Engine properties:**
    - Property: put then get returns same value (roundtrip)
    - Property: delete then get returns tombstone or not found
  - **Quorum properties:**
    - Property: W + R > N implies strong consistency
    - Property: write succeeds iff >= W acks received
  - Use Hypothesis strategies for generating random keys, values, and operations

- [x] 8.4 Write WAL crash recovery tests
  - File: `key-value-store/tests/test_storage/test_wal_recovery.py`
  - Test: partial write (simulated crash mid-append) is detected and skipped
  - Test: replay after clean shutdown recovers all entries
  - Test: replay after crash recovers all complete entries
  - Test: truncate after flush means no replay on next start

---

## Task 9: Examples and Documentation

- [-] 9.1 Create demo script
  - File: `key-value-store/examples/demo.py`
  - Demonstrate: starting a 3-node cluster
  - Demonstrate: put/get/delete operations via client
  - Demonstrate: node failure and recovery
  - Demonstrate: conflict detection with concurrent writes
  - Include comments explaining each step
  - Runnable with `python examples/demo.py`

- [~] 9.2 Create README.md
  - File: `key-value-store/README.md`
  - Project overview and architecture diagram
  - Installation instructions (`pip install -e ".[dev]"`)
  - Quick start guide (single node and multi-node)
  - Configuration reference (all config dataclasses)
  - API reference (KVClient methods)
  - Design decisions and trade-offs
  - Proto regeneration instructions

- [~] 9.3 Move/update documentation files
  - File: `key-value-store/docs/design.md` — copy from `.kiro/specs/key-value-store/design.md`
  - File: `key-value-store/docs/requirements.md` — already exists, verify up to date
  - File: `key-value-store/docs/tasks.md` — copy from `.kiro/specs/key-value-store/tasks.md`

- [~] 9.4 Add inline code documentation
  - All public classes and methods have docstrings
  - Module-level docstrings explaining purpose
  - Type hints on all function signatures
  - Comments for non-obvious algorithms (bloom filter hashing, k-way merge, vector clock comparison)


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
