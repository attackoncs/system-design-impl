# 分布式键值存储

一个基于 Python 实现的分布式键值存储系统，设计灵感来源于《System Design Interview》第 7 章（Dynamo 风格架构）。支持一致性哈希、可调节仲裁一致性、向量时钟、基于 Gossip 协议的故障检测以及 LSM-tree 存储引擎。

> English documentation: [README.md](./README.md)

## 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        客户端层                                   │
│            (KVClient — put/get/delete 路由)                       │
├─────────────────────────────────────────────────────────────────┤
│                        协调层                                     │
│   (Request Coordinator — 仲裁写/读、冲突检测)                      │
├─────────────────────────────────────────────────────────────────┤
│                        复制层                                     │
│   (VectorClock 向量时钟、Quorum 仲裁逻辑、冲突解决)                 │
├──────────────────────┬──────────────────────────────────────────┤
│      集群层          │            网络层                          │
│ (Gossip 协议,        │    (gRPC 服务端/客户端,                    │
│  成员管理,           │     Protobuf 序列化)                       │
│  Hinted Handoff,     │                                          │
│  Merkle Tree)        │                                          │
├──────────────────────┴──────────────────────────────────────────┤
│                       分区层                                      │
│        (ConsistentHashRing — 来自 consistent-hashing 库)          │
├─────────────────────────────────────────────────────────────────┤
│                       存储层                                      │
│   (WAL → MemTable → SSTable, 布隆过滤器, 压缩合并)                 │
└─────────────────────────────────────────────────────────────────┘
```

### 请求流程

```
客户端请求 (put/get/delete)
        │
        ▼
┌──────────────────┐
│   任意节点        │ ◄── 客户端连接任意节点（去中心化）
│  (协调者)         │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐     ┌──────────────────┐
│ 一致性哈希环      │────▶│  确定 N 个        │
│ (get_nodes)      │     │  副本节点          │
└──────────────────┘     └────────┬─────────┘
                                  │
                    ┌─────────────┼─────────────┐
                    ▼             ▼             ▼
              ┌──────────┐ ┌──────────┐ ┌──────────┐
              │ 副本 1   │ │ 副本 2   │ │ 副本 3   │
              │  (gRPC)  │ │  (gRPC)  │ │  (gRPC)  │
              └─────┬────┘ └─────┬────┘ └─────┬────┘
                    │            │            │
                    ▼            ▼            ▼
              ┌──────────┐ ┌──────────┐ ┌──────────┐
              │WAL→Mem→SS│ │WAL→Mem→SS│ │WAL→Mem→SS│
              └──────────┘ └──────────┘ └──────────┘
                    │            │            │
                    └────────────┼────────────┘
                                 ▼
                   等待 W/R 个确认响应
                   (仲裁满足 → 返回结果)
```

## 核心特性

- **一致性哈希**：数据分区（复用 `consistent-hashing` 库）
- **可调节一致性**：通过仲裁共识实现（可配置 N、W、R）
- **向量时钟**：冲突检测与因果排序
- **Gossip 协议**：去中心化故障检测
- **Hinted Handoff**：临时故障处理（宽松仲裁）
- **Merkle 树**：副本间反熵修复
- **LSM-tree 存储引擎**：WAL → MemTable → SSTable（含布隆过滤器）
- **gRPC 通信**：节点间高效通信（Protocol Buffers）
- **完全去中心化**：无单点故障

## 安装

```bash
# 克隆仓库
git clone <repo-url>
cd key-value-store

# 以开发模式安装（含所有依赖）
pip install -e ".[dev]"
```

环境要求：
- Python >= 3.9
- `consistent-hashing` 库（同级目录，自动安装）

## 快速开始

### 单节点模式

```python
import asyncio
from kv_store import KVNode, NodeConfig
from kv_store.config import StorageConfig, NetworkConfig

async def main():
    config = NodeConfig(
        node_id="node-1",
        storage=StorageConfig(data_dir="./data/node1"),
        network=NetworkConfig(port=50051),
    )

    node = KVNode(config)
    await node.start()

    # 写入
    result = await node.put("hello", b"world")
    print(f"写入成功: {result.success}")

    # 读取
    result = await node.get("hello")
    print(f"值: {result.value}")  # b"world"

    # 删除
    result = await node.delete("hello")
    print(f"删除成功: {result.success}")

    await node.stop()

asyncio.run(main())
```

### 多节点集群

```python
import asyncio
from kv_store import KVNode, NodeConfig
from kv_store.config import StorageConfig, NetworkConfig, ClusterConfig

async def main():
    # 启动 3 个节点
    nodes = []
    for i in range(3):
        config = NodeConfig(
            node_id=f"node-{i+1}",
            storage=StorageConfig(data_dir=f"./data/node{i+1}"),
            network=NetworkConfig(port=50051 + i),
            cluster=ClusterConfig(
                seed_nodes=["localhost:50051"] if i > 0 else [],
            ),
        )
        node = KVNode(config)
        await node.start()
        nodes.append(node)

    # 使用客户端与集群交互
    from kv_store.client import KVClient, ClientConfig

    client_config = ClientConfig(
        seed_nodes=["localhost:50051", "localhost:50052", "localhost:50053"],
    )

    async with KVClient(client_config) as client:
        await client.put("user:1", b'{"name": "Alice"}')
        resp = await client.get("user:1")
        print(f"值: {resp.value}")

    # 关闭集群
    for node in reversed(nodes):
        await node.stop()

asyncio.run(main())
```

### 运行示例

```bash
python examples/demo.py
```

## 配置参考

所有配置通过 dataclass 实现，提供合理的默认值。

### `NodeConfig` 节点配置

顶层配置，组合所有子配置。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `node_id` | `str` | `"node-1"` | 节点唯一标识 |
| `storage` | `StorageConfig` | （见下文） | 存储引擎配置 |
| `replication` | `ReplicationConfig` | （见下文） | 仲裁/复制配置 |
| `cluster` | `ClusterConfig` | （见下文） | 集群成员配置 |
| `network` | `NetworkConfig` | （见下文） | gRPC 网络配置 |
| `virtual_nodes` | `int` | `150` | 每个物理节点的虚拟节点数 |

### `StorageConfig` 存储配置

LSM-tree 存储引擎配置。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `data_dir` | `str` | `"./data"` | 数据根目录 |
| `wal_dir` | `str` | `"./data/wal"` | 预写日志目录 |
| `sstable_dir` | `str` | `"./data/sstables"` | SSTable 文件目录 |
| `memtable_size_bytes` | `int` | `4194304`（4 MB） | MemTable 刷盘阈值 |
| `bloom_filter_fp_rate` | `float` | `0.01` | 布隆过滤器误判率 |
| `compaction_threshold` | `int` | `4` | 触发压缩的 SSTable 数量 |

### `ReplicationConfig` 复制配置

仲裁与复制参数。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `n_replicas` | `int` | `3` | 每个键的总副本数 |
| `w_quorum` | `int` | `2` | 写仲裁（需要的确认数） |
| `r_quorum` | `int` | `2` | 读仲裁（需要的响应数） |
| `vector_clock_max_entries` | `int` | `10` | 向量时钟最大条目数 |

### `ClusterConfig` 集群配置

集群成员与协议配置。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `gossip_interval_seconds` | `float` | `1.0` | Gossip 轮次间隔 |
| `gossip_fanout` | `int` | `3` | 每轮联系的节点数 |
| `failure_timeout_seconds` | `float` | `5.0` | 标记节点疑似故障的超时 |
| `hinted_handoff_interval_seconds` | `float` | `10.0` | 提示数据投递检查间隔 |
| `anti_entropy_interval_seconds` | `float` | `60.0` | Merkle 树同步间隔 |
| `merkle_tree_buckets` | `int` | `1024` | Merkle 树桶数量 |
| `seed_nodes` | `list[str]` | `[]` | 种子节点地址列表 |

### `NetworkConfig` 网络配置

gRPC 网络配置。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `host` | `str` | `"0.0.0.0"` | 绑定地址 |
| `port` | `int` | `50051` | gRPC 端口 |
| `max_message_size_bytes` | `int` | `16777216`（16 MB） | 最大 gRPC 消息大小 |

## API 参考

### `KVClient` 客户端

与集群交互的主要客户端接口。

```python
from kv_store.client import KVClient, ClientConfig

config = ClientConfig(
    seed_nodes=["localhost:50051"],
    timeout=5.0,        # RPC 超时（秒）
    retry_count=3,      # 每个节点的重试次数
    retry_delay=0.5,    # 重试间隔（秒）
)
```

#### 方法

| 方法 | 签名 | 说明 |
|------|------|------|
| `connect()` | `async def connect() -> None` | 连接到种子节点 |
| `close()` | `async def close() -> None` | 关闭连接 |
| `put()` | `async def put(key, value, consistency?, vector_clock?) -> KVResponse` | 存储键值对 |
| `get()` | `async def get(key, consistency?) -> KVResponse` | 获取值 |
| `delete()` | `async def delete(key, consistency?, vector_clock?) -> KVResponse` | 删除键（墓碑标记） |

#### `KVResponse` 响应

| 字段 | 类型 | 说明 |
|------|------|------|
| `success` | `bool` | 操作是否成功 |
| `value` | `Optional[bytes]` | 值（用于 get 操作） |
| `vector_clock` | `Optional[VectorClock]` | 版本信息 |
| `has_conflict` | `bool` | 是否存在冲突版本 |
| `conflicting_values` | `list[tuple[bytes, VectorClock]]` | 所有冲突版本 |

#### 一致性级别

通过 `consistency` 参数传递给 `put()`、`get()` 或 `delete()`：

| 级别 | W | R | 保证 |
|------|---|---|------|
| `"one"` | 1 | 1 | 最终一致性，最低延迟 |
| `"quorum"` | 2 | 2 | 强一致性（N=3 时） |
| `"all"` | N | N | 最强一致性，最低可用性 |

### `KVNode` 节点

节点编排器 — 用于嵌入式场景或测试。

```python
from kv_store import KVNode, NodeConfig

node = KVNode(config)
await node.start()

# 直接操作（绕过 gRPC，使用本地协调器）
await node.put("key", b"value")
result = await node.get("key")
await node.delete("key")

await node.stop()
```

## 核心组件详解

### 存储引擎（LSM-Tree）

采用日志结构合并树（LSM-Tree）架构，针对写密集型工作负载优化：

```
写入路径: WAL（预写日志）→ MemTable（内存排序表）→ SSTable（磁盘排序表）
读取路径: MemTable → 布隆过滤器 → SSTable（从新到旧）
```

| 组件 | 职责 |
|------|------|
| **WAL（预写日志）** | 崩溃恢复保障，所有写入先持久化到 WAL |
| **MemTable** | 内存中的有序键值表，支持 O(log N) 操作 |
| **SSTable** | 不可变的磁盘排序文件，含稀疏索引 |
| **布隆过滤器** | 快速判断键是否可能存在于 SSTable 中 |
| **压缩管理器** | 合并多个 SSTable，删除过期数据 |

### 向量时钟

用于追踪事件的因果顺序，检测并发冲突：

- **支配关系**：时钟 A 支配时钟 B 表示 A 因果上更新
- **冲突检测**：两个时钟互不支配时存在并发冲突
- **自动解决**：一个版本支配另一个时保留较新版本
- **客户端解决**：存在冲突时返回所有版本由客户端决定

### Gossip 协议

去中心化的故障检测机制：

1. 每个节点定期递增自身心跳计数器
2. 随机选择若干节点交换成员列表
3. 合并时取每个节点的最大心跳值
4. 超时未更新的节点标记为疑似故障
5. 持续超时后标记为宕机

### Hinted Handoff（提示转交）

处理临时节点故障的机制：

1. 目标副本不可用时，写入转发到环上下一个健康节点
2. 替代节点存储数据并附带目标节点提示
3. 目标节点恢复后，提示数据被推送回去
4. 成功转交后删除提示数据

### Merkle 树（反熵）

副本间数据一致性修复：

1. 每个节点维护键空间的 Merkle 树
2. 定期与副本节点比较树根哈希
3. 哈希不同时逐层定位差异桶
4. 仅同步存在差异的数据

## 设计决策与权衡

| 决策 | 理由 | 权衡 |
|------|------|------|
| **Dynamo 风格（AP）** | 优先保证可用性和分区容错 | 需要应用层冲突解决 |
| **向量时钟** | 精确的因果排序检测 | 每个键的空间开销；裁剪可能丢失历史 |
| **宽松仲裁** | 故障期间维持可用性 | 转交完成前存在临时不一致 |
| **LSM-tree 存储** | 写密集型工作负载优化 | 读放大（通过布隆过滤器缓解） |
| **Gossip 协议** | 去中心化，成员管理无单点故障 | 最终传播（秒级，非即时） |
| **gRPC** | 高效二进制协议，代码自动生成 | 需要 protobuf 工具链；可读性较差 |
| **一致性哈希** | 拓扑变化时最小化数据迁移 | 需要虚拟节点保证均衡 |
| **大小分层压缩** | 简单，适合写密集负载 | 压缩期间空间放大 |

## Proto 文件重新生成

修改 `proto/kvstore.proto` 后，重新生成 Python 桩代码：

```bash
python -m grpc_tools.protoc \
    -I proto \
    --python_out=src/kv_store/network \
    --grpc_python_out=src/kv_store/network \
    proto/kvstore.proto
```

## 开发指南

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行全部测试
pytest

# 详细输出
pytest -v

# 仅运行属性测试
pytest tests/test_properties.py -v

# 运行特定模块测试
pytest tests/test_storage/ -v

# 运行集成测试
pytest tests/test_integration.py tests/test_multi_node.py -v
```

## 项目结构

```
key-value-store/
├── pyproject.toml              # 构建配置（hatchling）
├── README.md                   # 英文文档
├── README_CN.md                # 中文文档（本文件）
├── proto/
│   └── kvstore.proto           # gRPC 服务定义
├── src/kv_store/
│   ├── __init__.py             # 包导出
│   ├── client.py               # KVClient 客户端 API
│   ├── config.py               # 配置 dataclass
│   ├── node.py                 # KVNode 节点编排器
│   ├── storage/                # LSM-tree 存储引擎
│   │   ├── engine.py           # StorageEngine（编排器）
│   │   ├── wal.py              # 预写日志
│   │   ├── memtable.py         # 内存排序表
│   │   ├── sstable.py          # 磁盘排序表
│   │   ├── bloom_filter.py     # 布隆过滤器
│   │   └── compaction.py       # SSTable 压缩合并
│   ├── replication/            # 仲裁与版本控制
│   │   ├── coordinator.py      # 请求协调器
│   │   ├── vector_clock.py     # 向量时钟
│   │   └── quorum.py           # 仲裁逻辑
│   ├── cluster/                # 成员管理与修复
│   │   ├── gossip.py           # Gossip 协议
│   │   ├── membership.py       # 集群成员管理
│   │   ├── hinted_handoff.py   # 提示转交
│   │   └── merkle_tree.py      # Merkle 树 / 反熵
│   └── network/                # gRPC 通信
│       ├── grpc_server.py      # gRPC 服务端
│       ├── grpc_client.py      # gRPC 客户端
│       ├── kvstore_pb2.py      # 生成的 protobuf 代码
│       └── kvstore_pb2_grpc.py # 生成的 gRPC 桩代码
├── tests/                      # 单元测试、集成测试、属性测试
├── examples/
│   └── demo.py                 # 交互式演示
└── docs/
    ├── design.md               # 详细设计文档
    ├── requirements.md         # 功能与非功能需求
    └── tasks.md                # 实现任务分解
```

## License

MIT
