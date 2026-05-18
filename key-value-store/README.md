# Distributed Key-Value Store

A distributed key-value store implemented in Python, based on the design principles from *System Design Interview* Chapter 7 (Dynamo-style architecture). Features consistent hashing, tunable quorum consistency, vector clocks, gossip-based failure detection, and LSM-tree storage.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Client Layer                              │
│            (KVClient — put/get/delete with routing)              │
├─────────────────────────────────────────────────────────────────┤
│                     Coordination Layer                           │
│   (Request Coordinator — quorum writes/reads, conflict detect)  │
├─────────────────────────────────────────────────────────────────┤
│                     Replication Layer                            │
│   (VectorClock, Quorum Logic, Conflict Resolution)              │
├──────────────────────┬──────────────────────────────────────────┤
│   Cluster Layer      │          Network Layer                   │
│ (Gossip Protocol,    │    (gRPC Server/Client,                  │
│  Membership,         │     Protobuf Serialization)              │
│  Hinted Handoff,     │                                          │
│  Merkle Tree)        │                                          │
├──────────────────────┴──────────────────────────────────────────┤
│                   Partitioning Layer                             │
│        (ConsistentHashRing — from consistent-hashing lib)       │
├─────────────────────────────────────────────────────────────────┤
│                     Storage Layer                                │
│   (WAL → MemTable → SSTable, Bloom Filters, Compaction)         │
└─────────────────────────────────────────────────────────────────┘
```

### Request Flow

```
Client Request (put/get/delete)
        │
        ▼
┌──────────────────┐
│   Any Node       │ ◄── Client connects to any node (decentralized)
│  (Coordinator)   │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐     ┌──────────────────┐
│ ConsistentHash   │────▶│  Determine N     │
│ Ring (get_nodes) │     │  Replica Nodes   │
└──────────────────┘     └────────┬─────────┘
                                  │
                    ┌─────────────┼─────────────┐
                    ▼             ▼             ▼
              ┌──────────┐ ┌──────────┐ ┌──────────┐
              │ Replica 1│ │ Replica 2│ │ Replica 3│
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
                   Wait for W/R acknowledgments
                   (Quorum satisfied → respond)
```

## Features

- **Consistent hashing** for data partitioning (reuses `consistent-hashing` library)
- **Tunable consistency** via quorum consensus (configurable N, W, R)
- **Vector clocks** for conflict detection and causal ordering
- **Gossip protocol** for decentralized failure detection
- **Hinted handoff** for temporary failure handling (sloppy quorum)
- **Merkle trees** for anti-entropy repair between replicas
- **LSM-tree storage engine** (WAL → MemTable → SSTable with Bloom filters)
- **gRPC** for inter-node communication (Protocol Buffers)
- **Fully decentralized** — no single point of failure

## Installation

```bash
# Clone the repository
git clone <repo-url>
cd key-value-store

# Install in development mode with all dependencies
pip install -e ".[dev]"
```

Requirements:
- Python >= 3.9
- The `consistent-hashing` library (sibling directory, installed automatically)

## Quick Start

### Single Node

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

    # Write a value
    result = await node.put("hello", b"world")
    print(f"Put success: {result.success}")

    # Read it back
    result = await node.get("hello")
    print(f"Value: {result.value}")  # b"world"

    # Delete
    result = await node.delete("hello")
    print(f"Delete success: {result.success}")

    await node.stop()

asyncio.run(main())
```

### Multi-Node Cluster

```python
import asyncio
from kv_store import KVNode, NodeConfig
from kv_store.config import StorageConfig, NetworkConfig, ClusterConfig

async def main():
    # Start 3 nodes
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

    # Use the client to interact with the cluster
    from kv_store.client import KVClient, ClientConfig

    client_config = ClientConfig(
        seed_nodes=["localhost:50051", "localhost:50052", "localhost:50053"],
    )

    async with KVClient(client_config) as client:
        await client.put("user:1", b'{"name": "Alice"}')
        resp = await client.get("user:1")
        print(f"Value: {resp.value}")

    # Shutdown
    for node in reversed(nodes):
        await node.stop()

asyncio.run(main())
```

### Running the Demo

```bash
python examples/demo.py
```

## Configuration Reference

All configuration is done via dataclasses with sensible defaults.

### `NodeConfig`

Top-level configuration combining all sub-configs.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `node_id` | `str` | `"node-1"` | Unique identifier for this node |
| `storage` | `StorageConfig` | (see below) | Storage engine settings |
| `replication` | `ReplicationConfig` | (see below) | Quorum/replication settings |
| `cluster` | `ClusterConfig` | (see below) | Cluster membership settings |
| `network` | `NetworkConfig` | (see below) | gRPC network settings |
| `virtual_nodes` | `int` | `150` | Virtual nodes per physical node on hash ring |

### `StorageConfig`

LSM-tree storage engine configuration.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `data_dir` | `str` | `"./data"` | Base data directory |
| `wal_dir` | `str` | `"./data/wal"` | Write-ahead log directory |
| `sstable_dir` | `str` | `"./data/sstables"` | SSTable files directory |
| `memtable_size_bytes` | `int` | `4194304` (4 MB) | MemTable flush threshold |
| `bloom_filter_fp_rate` | `float` | `0.01` | Bloom filter false positive rate |
| `compaction_threshold` | `int` | `4` | SSTables at a level before compaction |

### `ReplicationConfig`

Quorum and replication configuration.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `n_replicas` | `int` | `3` | Total replicas per key |
| `w_quorum` | `int` | `2` | Write quorum (acks needed) |
| `r_quorum` | `int` | `2` | Read quorum (responses needed) |
| `vector_clock_max_entries` | `int` | `10` | Max vector clock entries before pruning |

### `ClusterConfig`

Cluster membership and protocol configuration.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `gossip_interval_seconds` | `float` | `1.0` | Gossip round interval |
| `gossip_fanout` | `int` | `3` | Peers contacted per gossip round |
| `failure_timeout_seconds` | `float` | `5.0` | Time before marking node suspected |
| `hinted_handoff_interval_seconds` | `float` | `10.0` | Hint delivery check interval |
| `anti_entropy_interval_seconds` | `float` | `60.0` | Merkle tree sync interval |
| `merkle_tree_buckets` | `int` | `1024` | Merkle tree bucket count |
| `seed_nodes` | `list[str]` | `[]` | Seed node addresses for joining |

### `NetworkConfig`

gRPC network configuration.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | `str` | `"0.0.0.0"` | Bind address |
| `port` | `int` | `50051` | gRPC port |
| `max_message_size_bytes` | `int` | `16777216` (16 MB) | Max gRPC message size |

## API Reference

### `KVClient`

The primary client interface for interacting with the cluster.

```python
from kv_store.client import KVClient, ClientConfig

config = ClientConfig(
    seed_nodes=["localhost:50051"],
    timeout=5.0,        # RPC timeout in seconds
    retry_count=3,      # Retries per node on UNAVAILABLE
    retry_delay=0.5,    # Delay between retries
)
```

#### Methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `connect()` | `async def connect() -> None` | Connect to a seed node |
| `close()` | `async def close() -> None` | Close the connection |
| `put()` | `async def put(key, value, consistency?, vector_clock?) -> KVResponse` | Store a key-value pair |
| `get()` | `async def get(key, consistency?) -> KVResponse` | Retrieve a value |
| `delete()` | `async def delete(key, consistency?, vector_clock?) -> KVResponse` | Delete a key (tombstone) |

#### `KVResponse`

| Field | Type | Description |
|-------|------|-------------|
| `success` | `bool` | Whether the operation succeeded |
| `value` | `Optional[bytes]` | The value (for get operations) |
| `vector_clock` | `Optional[VectorClock]` | Version information |
| `has_conflict` | `bool` | Whether conflicting versions exist |
| `conflicting_values` | `list[tuple[bytes, VectorClock]]` | All conflicting versions |

#### Consistency Levels

Pass as the `consistency` parameter to `put()`, `get()`, or `delete()`:

| Level | W | R | Guarantees |
|-------|---|---|------------|
| `"one"` | 1 | 1 | Eventual consistency, lowest latency |
| `"quorum"` | 2 | 2 | Strong consistency (with N=3) |
| `"all"` | N | N | Strongest consistency, lowest availability |

### `KVNode`

The node orchestrator — use directly for embedded scenarios or testing.

```python
from kv_store import KVNode, NodeConfig

node = KVNode(config)
await node.start()

# Direct operations (bypasses gRPC, uses local coordinator)
await node.put("key", b"value")
result = await node.get("key")
await node.delete("key")

await node.stop()
```

## Design Decisions & Trade-offs

| Decision | Rationale | Trade-off |
|----------|-----------|-----------|
| **Dynamo-style (AP)** | Prioritizes availability and partition tolerance | Requires conflict resolution at application level |
| **Vector clocks** | Precise causal ordering detection | Space overhead per key; pruning may lose history |
| **Sloppy quorum** | Maintains availability during failures | Temporary inconsistency until handoff completes |
| **LSM-tree storage** | Optimized for write-heavy workloads | Read amplification (mitigated by Bloom filters) |
| **Gossip protocol** | Decentralized, no SPOF for membership | Eventual propagation (seconds, not instant) |
| **gRPC** | Efficient binary protocol, code generation | Requires protobuf tooling; less human-readable |
| **Consistent hashing** | Minimal data movement on topology changes | Requires virtual nodes for balance |
| **Size-tiered compaction** | Simple, good for write-heavy loads | Space amplification during compaction |

## Proto Regeneration

If you modify `proto/kvstore.proto`, regenerate the Python stubs:

```bash
python -m grpc_tools.protoc \
    -I proto \
    --python_out=src/kv_store/network \
    --grpc_python_out=src/kv_store/network \
    proto/kvstore.proto
```

Or use the shorthand (from the `key-value-store/` directory):

```bash
python -m grpc_tools.protoc -Iproto --python_out=src/kv_store/network --grpc_python_out=src/kv_store/network proto/kvstore.proto
```

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run all tests
pytest

# Run tests with verbose output
pytest -v

# Run only property-based tests
pytest tests/test_properties.py -v

# Run specific test module
pytest tests/test_storage/ -v
```

## Project Structure

```
key-value-store/
├── pyproject.toml              # Build config (hatchling)
├── README.md                   # This file
├── proto/
│   └── kvstore.proto           # gRPC service definition
├── src/kv_store/
│   ├── __init__.py             # Package exports
│   ├── client.py               # KVClient API
│   ├── config.py               # Configuration dataclasses
│   ├── node.py                 # KVNode orchestrator
│   ├── storage/                # LSM-tree storage engine
│   │   ├── engine.py           # StorageEngine (orchestrator)
│   │   ├── wal.py              # Write-ahead log
│   │   ├── memtable.py         # In-memory sorted table
│   │   ├── sstable.py          # Sorted string table (disk)
│   │   ├── bloom_filter.py     # Bloom filter
│   │   └── compaction.py       # SSTable compaction
│   ├── replication/            # Quorum & versioning
│   │   ├── coordinator.py      # Request coordinator
│   │   ├── vector_clock.py     # Vector clock
│   │   └── quorum.py           # Quorum logic
│   ├── cluster/                # Membership & repair
│   │   ├── gossip.py           # Gossip protocol
│   │   ├── membership.py       # Cluster membership
│   │   ├── hinted_handoff.py   # Hinted handoff
│   │   └── merkle_tree.py      # Merkle tree / anti-entropy
│   └── network/                # gRPC communication
│       ├── grpc_server.py      # gRPC server
│       ├── grpc_client.py      # gRPC client
│       ├── kvstore_pb2.py      # Generated protobuf
│       └── kvstore_pb2_grpc.py # Generated gRPC stubs
├── tests/                      # Unit, integration, property tests
├── examples/
│   └── demo.py                 # Interactive demonstration
└── docs/
    ├── design.md               # Detailed design document
    ├── requirements.md         # Functional & non-functional requirements
    └── tasks.md                # Implementation task breakdown
```

## License

MIT
