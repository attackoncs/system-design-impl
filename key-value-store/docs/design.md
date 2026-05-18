# Design: Distributed Key-Value Store

## Architecture Overview

The distributed key-value store follows a layered architecture inspired by Amazon Dynamo. Each node is self-contained with identical responsibilities, communicating via gRPC. Data is partitioned using consistent hashing, replicated across N nodes with tunable quorum consistency, and persisted via an LSM-tree storage engine.

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
                    └─────────────┼─────────────┘
                                  ▼
                    Wait for W/R acknowledgments
                    (Quorum satisfied → respond)
```

## Project Structure

```
key-value-store/
├── pyproject.toml
├── README.md
├── docs/
│   ├── requirements.md
│   ├── design.md
│   └── tasks.md
├── proto/
│   └── kvstore.proto
├── src/
│   └── kv_store/
│       ├── __init__.py
│       ├── client.py           # Client API
│       ├── node.py             # Node orchestrator
│       ├── config.py           # Configuration
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── engine.py       # LSM-tree storage engine
│       │   ├── wal.py          # Write-ahead log
│       │   ├── memtable.py     # In-memory sorted table
│       │   ├── sstable.py      # Sorted string table
│       │   ├── bloom_filter.py # Bloom filter
│       │   └── compaction.py   # SSTable compaction
│       ├── replication/
│       │   ├── __init__.py
│       │   ├── coordinator.py  # Request coordination
│       │   ├── vector_clock.py # Vector clock implementation
│       │   └── quorum.py       # Quorum logic
│       ├── cluster/
│       │   ├── __init__.py
│       │   ├── gossip.py       # Gossip protocol
│       │   ├── membership.py   # Cluster membership
│       │   ├── hinted_handoff.py # Hinted handoff
│       │   └── merkle_tree.py  # Anti-entropy
│       └── network/
│           ├── __init__.py
│           ├── grpc_server.py  # gRPC server
│           └── grpc_client.py  # gRPC client
├── tests/
│   ├── __init__.py
│   ├── test_storage/
│   │   ├── __init__.py
│   │   ├── test_wal.py
│   │   ├── test_memtable.py
│   │   ├── test_sstable.py
│   │   ├── test_bloom_filter.py
│   │   └── test_compaction.py
│   ├── test_replication/
│   │   ├── __init__.py
│   │   ├── test_vector_clock.py
│   │   ├── test_coordinator.py
│   │   └── test_quorum.py
│   ├── test_cluster/
│   │   ├── __init__.py
│   │   ├── test_gossip.py
│   │   ├── test_membership.py
│   │   ├── test_hinted_handoff.py
│   │   └── test_merkle_tree.py
│   └── test_properties.py
└── examples/
    └── demo.py
```

## Component Design

### 1. Configuration

```python
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StorageConfig:
    """LSM-tree storage engine configuration."""
    data_dir: str = "./data"
    wal_dir: str = "./data/wal"
    sstable_dir: str = "./data/sstables"
    memtable_size_bytes: int = 4 * 1024 * 1024  # 4 MB flush threshold
    bloom_filter_fp_rate: float = 0.01           # 1% false positive rate
    compaction_threshold: int = 4                 # merge after N SSTables


@dataclass
class ReplicationConfig:
    """Quorum and replication configuration."""
    n_replicas: int = 3          # total replicas per key
    w_quorum: int = 2            # write quorum
    r_quorum: int = 2            # read quorum
    vector_clock_max_entries: int = 10  # prune oldest entries beyond this


@dataclass
class ClusterConfig:
    """Cluster membership and protocol configuration."""
    gossip_interval_seconds: float = 1.0
    gossip_fanout: int = 3                      # number of peers per gossip round
    failure_timeout_seconds: float = 5.0
    hinted_handoff_interval_seconds: float = 10.0
    anti_entropy_interval_seconds: float = 60.0
    merkle_tree_buckets: int = 1024
    seed_nodes: list[str] = field(default_factory=list)


@dataclass
class NetworkConfig:
    """gRPC network configuration."""
    host: str = "0.0.0.0"
    port: int = 50051
    max_message_size_bytes: int = 16 * 1024 * 1024  # 16 MB


@dataclass
class NodeConfig:
    """Top-level node configuration combining all sub-configs."""
    node_id: str = "node-1"
    storage: StorageConfig = field(default_factory=StorageConfig)
    replication: ReplicationConfig = field(default_factory=ReplicationConfig)
    cluster: ClusterConfig = field(default_factory=ClusterConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    virtual_nodes: int = 150  # virtual nodes per physical node on hash ring

```

### 2. Storage Engine — Write-Ahead Log (WAL)

```python
import asyncio
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class WALEntryType(Enum):
    """Type of WAL entry."""
    PUT = "PUT"
    DELETE = "DELETE"


@dataclass
class WALEntry:
    """A single entry in the write-ahead log."""
    sequence_number: int
    entry_type: WALEntryType
    key: str
    value: Optional[bytes]  # None for DELETE entries
    timestamp: float


class WriteAheadLog:
    """Append-only write-ahead log for crash recovery.

    All writes are persisted to the WAL before being applied to the MemTable.
    On recovery, the WAL is replayed to reconstruct the MemTable state.
    The WAL is truncated after a successful MemTable flush to SSTable.

    Covers: FR-9.1, NFR-2.1, NFR-2.2, NFR-2.3
    """

    def __init__(self, wal_dir: str):
        """Initialize WAL with the given directory path.

        Args:
            wal_dir: Directory to store WAL segment files.
        """
        ...

    async def append(self, entry: WALEntry) -> None:
        """Append an entry to the WAL and fsync to disk.

        Args:
            entry: The WAL entry to persist.

        Raises:
            IOError: If the write or fsync fails.
        """
        ...

    async def replay(self) -> list[WALEntry]:
        """Replay all entries from the current WAL segment.

        Used during node startup to recover MemTable state.

        Returns:
            List of WAL entries in sequence order.
        """
        ...

    async def truncate(self) -> None:
        """Truncate the WAL after successful MemTable flush.

        Creates a new empty segment file.
        """
        ...

    @property
    def sequence_number(self) -> int:
        """Current sequence number (monotonically increasing)."""
        ...
```

### 3. Storage Engine — MemTable

```python
from dataclasses import dataclass
from typing import Optional, Iterator


@dataclass
class MemTableEntry:
    """An entry stored in the MemTable."""
    key: str
    value: Optional[bytes]  # None indicates a tombstone (delete)
    timestamp: float
    is_tombstone: bool = False


class MemTable:
    """In-memory sorted key-value table (red-black tree / sorted dict).

    Provides O(log N) insert and lookup. When the size threshold is reached,
    the MemTable is frozen and flushed to an SSTable on disk.

    Covers: FR-9.2, FR-9.3, FR-10.1
    """

    def __init__(self, size_threshold_bytes: int = 4 * 1024 * 1024):
        """Initialize MemTable with a flush size threshold.

        Args:
            size_threshold_bytes: Flush to SSTable when this size is exceeded.
        """
        ...

    def put(self, key: str, value: bytes, timestamp: float) -> None:
        """Insert or update a key-value pair.

        Args:
            key: The key string.
            value: The value bytes.
            timestamp: Write timestamp for ordering.
        """
        ...

    def delete(self, key: str, timestamp: float) -> None:
        """Mark a key as deleted with a tombstone.

        Args:
            key: The key to delete.
            timestamp: Deletion timestamp.
        """
        ...

    def get(self, key: str) -> Optional[MemTableEntry]:
        """Look up a key in the MemTable.

        Args:
            key: The key to look up.

        Returns:
            The MemTableEntry if found, None otherwise.
        """
        ...

    def is_full(self) -> bool:
        """Check if the MemTable has exceeded its size threshold."""
        ...

    @property
    def size_bytes(self) -> int:
        """Current approximate size in bytes."""
        ...

    def entries_sorted(self) -> Iterator[MemTableEntry]:
        """Iterate all entries in sorted key order (for flushing to SSTable)."""
        ...

    def clear(self) -> None:
        """Clear all entries (after successful flush)."""
        ...
```

### 4. Storage Engine — SSTable

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Iterator


@dataclass
class SSTableMetadata:
    """Metadata for an SSTable file."""
    file_path: str
    level: int
    min_key: str
    max_key: str
    entry_count: int
    size_bytes: int
    created_at: float


@dataclass
class SSTableEntry:
    """A single entry in an SSTable."""
    key: str
    value: Optional[bytes]
    timestamp: float
    is_tombstone: bool


class SSTable:
    """Immutable sorted string table stored on disk.

    SSTables are created by flushing a MemTable. They contain sorted
    key-value pairs with a sparse index for efficient binary search lookups.
    Each SSTable has an associated Bloom filter for fast negative lookups.

    File format:
        [data block] [index block] [metadata block] [footer]

    Covers: FR-9.4, FR-10.3
    """

    def __init__(self, file_path: str):
        """Open an existing SSTable file for reading.

        Args:
            file_path: Path to the SSTable file on disk.
        """
        ...

    @classmethod
    async def create_from_memtable(
        cls, memtable: "MemTable", file_path: str, level: int = 0
    ) -> "SSTable":
        """Flush a MemTable to a new SSTable file on disk.

        Args:
            memtable: The MemTable to flush.
            file_path: Destination file path.
            level: Compaction level (0 for freshly flushed).

        Returns:
            The newly created SSTable instance.
        """
        ...

    async def get(self, key: str) -> Optional[SSTableEntry]:
        """Look up a key using the sparse index and binary search.

        Args:
            key: The key to look up.

        Returns:
            The SSTableEntry if found, None otherwise.
        """
        ...

    def may_contain(self, key: str) -> bool:
        """Check the Bloom filter for possible key existence.

        Args:
            key: The key to check.

        Returns:
            True if the key might exist, False if definitely not.
        """
        ...

    def entries(self) -> Iterator[SSTableEntry]:
        """Iterate all entries in sorted order (for compaction)."""
        ...

    @property
    def metadata(self) -> SSTableMetadata:
        """Get SSTable metadata."""
        ...
```

### 5. Storage Engine — Bloom Filter

```python
import math
from typing import Optional


class BloomFilter:
    """Space-efficient probabilistic data structure for set membership testing.

    Used to avoid unnecessary SSTable disk reads. A negative result guarantees
    the key is not in the SSTable; a positive result means the key might be present.

    Covers: FR-10.2, FR-10.4
    """

    def __init__(
        self,
        expected_items: int,
        false_positive_rate: float = 0.01,
    ):
        """Initialize a Bloom filter with optimal size and hash count.

        Args:
            expected_items: Expected number of items to insert.
            false_positive_rate: Target false positive probability (default 1%).

        The bit array size and number of hash functions are computed
        from these parameters using the optimal formulas:
            m = -n * ln(p) / (ln(2))^2
            k = (m / n) * ln(2)
        """
        ...

    def add(self, key: str) -> None:
        """Add a key to the Bloom filter.

        Args:
            key: The key to add.
        """
        ...

    def might_contain(self, key: str) -> bool:
        """Check if a key might be in the set.

        Args:
            key: The key to check.

        Returns:
            False means definitely not present.
            True means possibly present (subject to false positive rate).
        """
        ...

    def serialize(self) -> bytes:
        """Serialize the Bloom filter to bytes for storage in SSTable footer."""
        ...

    @classmethod
    def deserialize(cls, data: bytes) -> "BloomFilter":
        """Deserialize a Bloom filter from bytes."""
        ...

    @property
    def size_bits(self) -> int:
        """Size of the bit array."""
        ...

    @property
    def num_hash_functions(self) -> int:
        """Number of hash functions used."""
        ...
```

### 6. Storage Engine — Compaction

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class CompactionStats:
    """Statistics from a compaction run."""
    input_sstables: int
    output_sstable: int
    keys_merged: int
    tombstones_removed: int
    bytes_before: int
    bytes_after: int


class CompactionManager:
    """Manages SSTable compaction using size-tiered compaction strategy.

    When the number of SSTables at a given level exceeds the threshold,
    they are merged into a single SSTable at the next level. During merge,
    duplicate keys are resolved by keeping the newest version, and expired
    tombstones are removed.

    Covers: FR-9.5
    """

    def __init__(self, sstable_dir: str, compaction_threshold: int = 4):
        """Initialize the compaction manager.

        Args:
            sstable_dir: Directory containing SSTable files.
            compaction_threshold: Number of SSTables at a level before compaction.
        """
        ...

    async def maybe_compact(self, sstables: list["SSTable"]) -> Optional["SSTable"]:
        """Check if compaction is needed and perform it if so.

        Args:
            sstables: Current list of SSTables.

        Returns:
            The new merged SSTable if compaction occurred, None otherwise.
        """
        ...

    async def compact(self, sstables: list["SSTable"], level: int) -> "SSTable":
        """Merge multiple SSTables into one, removing duplicates and tombstones.

        Uses a k-way merge of sorted iterators. For duplicate keys, keeps
        the entry with the newest timestamp. Tombstones older than a
        configurable grace period are removed.

        Args:
            sstables: SSTables to merge (must be at the same level).
            level: The target level for the output SSTable.

        Returns:
            The newly created merged SSTable.
        """
        ...

    def get_compaction_stats(self) -> CompactionStats:
        """Get statistics from the last compaction run."""
        ...
```

### 7. Storage Engine — Orchestrator

```python
from typing import Optional


@dataclass
class StorageResult:
    """Result of a storage operation."""
    key: str
    value: Optional[bytes]
    timestamp: float
    is_tombstone: bool
    found: bool


class StorageEngine:
    """LSM-tree storage engine orchestrating WAL, MemTable, SSTables.

    Write path: WAL → MemTable → (flush) → SSTable
    Read path:  MemTable → Bloom filter → SSTables (newest first)

    Covers: FR-9, FR-10
    """

    def __init__(self, config: "StorageConfig"):
        """Initialize the storage engine.

        Args:
            config: Storage configuration.
        """
        ...

    async def start(self) -> None:
        """Start the storage engine — replay WAL, load SSTables."""
        ...

    async def stop(self) -> None:
        """Gracefully stop — flush MemTable, close files."""
        ...

    async def put(self, key: str, value: bytes, timestamp: float) -> None:
        """Write a key-value pair.

        1. Append to WAL
        2. Insert into MemTable
        3. If MemTable full, flush to SSTable

        Args:
            key: Key string (max 256 bytes).
            value: Value bytes (max 10 KB).
            timestamp: Write timestamp.

        Raises:
            ValueError: If key or value exceeds size limits.
        """
        ...

    async def get(self, key: str) -> Optional[StorageResult]:
        """Read a key.

        1. Check MemTable
        2. Check Bloom filters on SSTables
        3. Search candidate SSTables newest-to-oldest

        Args:
            key: Key to look up.

        Returns:
            StorageResult if found (may be a tombstone), None if not found.
        """
        ...

    async def delete(self, key: str, timestamp: float) -> None:
        """Delete a key by writing a tombstone marker.

        Args:
            key: Key to delete.
            timestamp: Deletion timestamp.
        """
        ...

    async def _flush_memtable(self) -> None:
        """Flush the current MemTable to a new SSTable and truncate WAL."""
        ...
```

### 8. Replication — Vector Clock

```python
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ClockEntry:
    """A single entry in a vector clock."""
    node_id: str
    counter: int


class VectorClock:
    """Vector clock for tracking causal ordering of events.

    Each data item carries a vector clock as a list of (node_id, counter) pairs.
    Used to detect conflicts: if neither clock dominates the other, the versions
    are concurrent (conflict).

    Covers: FR-5.1, FR-5.2, FR-5.3, FR-5.4, FR-5.5, FR-5.6
    """

    def __init__(
        self,
        entries: Optional[dict[str, int]] = None,
        max_entries: int = 10,
    ):
        """Initialize a vector clock.

        Args:
            entries: Initial clock entries as {node_id: counter}.
            max_entries: Maximum entries before pruning oldest.
        """
        ...

    def increment(self, node_id: str) -> "VectorClock":
        """Increment the counter for a node, returning a new VectorClock.

        If the node doesn't exist in the clock, it is added with counter=1.
        If max_entries is exceeded, the oldest entry (lowest counter) is pruned.

        Args:
            node_id: The node performing the write.

        Returns:
            A new VectorClock with the incremented counter.
        """
        ...

    def merge(self, other: "VectorClock") -> "VectorClock":
        """Merge two vector clocks by taking the max counter for each node.

        Args:
            other: The other vector clock to merge with.

        Returns:
            A new VectorClock representing the merged state.
        """
        ...

    def dominates(self, other: "VectorClock") -> bool:
        """Check if this clock causally dominates (is strictly newer than) another.

        Clock A dominates Clock B if:
        - For every node in B, A has a counter >= B's counter
        - For at least one node, A has a counter > B's counter

        Args:
            other: The other vector clock to compare against.

        Returns:
            True if this clock dominates the other.
        """
        ...

    def conflicts_with(self, other: "VectorClock") -> bool:
        """Check if two clocks are concurrent (neither dominates).

        Args:
            other: The other vector clock.

        Returns:
            True if the clocks are concurrent (conflict exists).
        """
        ...

    def to_dict(self) -> dict[str, int]:
        """Serialize to a dictionary for storage/transmission."""
        ...

    @classmethod
    def from_dict(cls, data: dict[str, int], max_entries: int = 10) -> "VectorClock":
        """Deserialize from a dictionary."""
        ...

    def __eq__(self, other: object) -> bool:
        ...

    def __repr__(self) -> str:
        ...
```

### 9. Replication — Quorum Logic

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ConsistencyLevel(Enum):
    """Predefined consistency levels."""
    ONE = "one"           # W=1, R=1 (eventual consistency)
    QUORUM = "quorum"     # W=2, R=2 with N=3 (strong consistency)
    ALL = "all"           # W=N, R=N (strongest, lowest availability)


@dataclass
class QuorumConfig:
    """Quorum parameters for a single operation."""
    n: int  # total replicas
    w: int  # write quorum
    r: int  # read quorum

    def is_strongly_consistent(self) -> bool:
        """Check if W + R > N (guarantees overlap)."""
        return self.w + self.r > self.n

    @classmethod
    def from_consistency_level(cls, level: ConsistencyLevel, n: int = 3) -> "QuorumConfig":
        """Create QuorumConfig from a named consistency level.

        Args:
            level: The desired consistency level.
            n: Total replica count.

        Returns:
            QuorumConfig with appropriate W and R values.
        """
        ...


@dataclass
class QuorumResult:
    """Result of a quorum operation."""
    success: bool
    responses_received: int
    responses_required: int
    failed_nodes: list[str]
    values: list[tuple[bytes, "VectorClock"]]  # for reads: collected values


class QuorumManager:
    """Manages quorum logic for read and write operations.

    Determines how many acknowledgments are needed, tracks responses,
    and decides when a quorum is satisfied or has failed.

    Covers: FR-4.1, FR-4.2, FR-4.3, FR-4.4, FR-4.5, FR-4.6
    """

    def __init__(self, config: "ReplicationConfig"):
        """Initialize with replication configuration.

        Args:
            config: Replication config with N, W, R defaults.
        """
        ...

    def get_quorum_config(
        self, consistency: Optional[ConsistencyLevel] = None
    ) -> QuorumConfig:
        """Get the quorum config, optionally overriding with a consistency level.

        Args:
            consistency: Optional override for the default consistency.

        Returns:
            QuorumConfig to use for the operation.
        """
        ...

    async def write_quorum(
        self,
        key: str,
        value: bytes,
        vector_clock: "VectorClock",
        replica_nodes: list[str],
        quorum_config: QuorumConfig,
    ) -> QuorumResult:
        """Execute a write with quorum consensus.

        Sends write requests to all N replicas concurrently.
        Returns success once W acknowledgments are received.

        Args:
            key: The key being written.
            value: The value bytes.
            vector_clock: The vector clock for this write.
            replica_nodes: List of node IDs to replicate to.
            quorum_config: Quorum parameters for this operation.

        Returns:
            QuorumResult indicating success/failure.
        """
        ...

    async def read_quorum(
        self,
        key: str,
        replica_nodes: list[str],
        quorum_config: QuorumConfig,
    ) -> QuorumResult:
        """Execute a read with quorum consensus.

        Queries R replicas concurrently and returns the most recent value
        based on vector clock comparison.

        Args:
            key: The key to read.
            replica_nodes: List of node IDs to query.
            quorum_config: Quorum parameters for this operation.

        Returns:
            QuorumResult with collected values for conflict resolution.
        """
        ...
```

### 10. Replication — Request Coordinator

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class PutResult:
    """Result of a put operation."""
    success: bool
    vector_clock: "VectorClock"
    replicas_acknowledged: int


@dataclass
class GetResult:
    """Result of a get operation."""
    found: bool
    value: Optional[bytes]
    vector_clock: Optional["VectorClock"]
    has_conflict: bool
    conflicting_values: list[tuple[bytes, "VectorClock"]]  # if conflict


@dataclass
class DeleteResult:
    """Result of a delete operation."""
    success: bool
    vector_clock: "VectorClock"


class RequestCoordinator:
    """Coordinates client requests across replica nodes.

    Any node can act as coordinator. The coordinator:
    1. Determines replica nodes via consistent hash ring
    2. Forwards requests to replicas via gRPC
    3. Collects responses and applies quorum logic
    4. Resolves conflicts using vector clocks
    5. Falls back to sloppy quorum if nodes are unavailable

    Covers: FR-1, FR-2, FR-3, FR-4, FR-5, FR-7, FR-12.2
    """

    def __init__(
        self,
        node_id: str,
        hash_ring: "ConsistentHashRing",
        quorum_manager: "QuorumManager",
        grpc_client: "GRPCClient",
        storage_engine: "StorageEngine",
        membership: "ClusterMembership",
        hinted_handoff: "HintedHandoffManager",
    ):
        """Initialize the coordinator.

        Args:
            node_id: This node's identifier.
            hash_ring: The consistent hash ring for partitioning.
            quorum_manager: Quorum logic manager.
            grpc_client: gRPC client for inter-node communication.
            storage_engine: Local storage engine.
            membership: Cluster membership state.
            hinted_handoff: Hinted handoff manager for failed nodes.
        """
        ...

    async def put(
        self,
        key: str,
        value: bytes,
        client_clock: Optional["VectorClock"] = None,
        consistency: Optional["ConsistencyLevel"] = None,
    ) -> PutResult:
        """Coordinate a put operation across replicas.

        1. Determine N replica nodes from hash ring (get_nodes)
        2. Increment vector clock for this coordinator node
        3. Write locally if this node is a replica
        4. Forward to other replicas via gRPC
        5. If a replica is down, use sloppy quorum (next healthy node)
        6. Wait for W acknowledgments

        Args:
            key: Key to write (max 256 bytes).
            value: Value to write (max 10 KB).
            client_clock: Vector clock from client (for read-modify-write).
            consistency: Optional consistency level override.

        Returns:
            PutResult with success status and updated vector clock.

        Raises:
            QuorumNotMetError: If fewer than W nodes acknowledge.
            ValueError: If key/value exceeds size limits.
        """
        ...

    async def get(
        self,
        key: str,
        consistency: Optional["ConsistencyLevel"] = None,
    ) -> GetResult:
        """Coordinate a get operation across replicas.

        1. Determine N replica nodes from hash ring
        2. Query R replicas concurrently
        3. Compare vector clocks from responses
        4. If one version dominates, return it
        5. If conflict (concurrent versions), return all to client

        Args:
            key: Key to read.
            consistency: Optional consistency level override.

        Returns:
            GetResult with value(s) and conflict information.

        Raises:
            QuorumNotMetError: If fewer than R nodes respond.
        """
        ...

    async def delete(
        self,
        key: str,
        client_clock: Optional["VectorClock"] = None,
        consistency: Optional["ConsistencyLevel"] = None,
    ) -> DeleteResult:
        """Coordinate a delete operation (tombstone write).

        Follows the same flow as put() but writes a tombstone marker.

        Args:
            key: Key to delete.
            client_clock: Vector clock from client.
            consistency: Optional consistency level override.

        Returns:
            DeleteResult with success status.
        """
        ...

    def _get_replica_nodes(self, key: str) -> list[str]:
        """Get N replica nodes for a key, substituting unavailable nodes.

        Uses hash_ring.get_nodes() for primary replicas, then applies
        sloppy quorum by finding next healthy nodes if any are down.

        Args:
            key: The key to partition.

        Returns:
            List of node IDs (may include substitute nodes for sloppy quorum).
        """
        ...
```

### 11. Cluster — Gossip Protocol

```python
import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class NodeStatus(Enum):
    """Status of a node in the cluster."""
    ALIVE = "alive"
    SUSPECTED = "suspected"
    DOWN = "down"


@dataclass
class MemberInfo:
    """Information about a cluster member."""
    node_id: str
    address: str          # host:port
    heartbeat_counter: int = 0
    last_updated: float = 0.0
    status: NodeStatus = NodeStatus.ALIVE


class GossipProtocol:
    """Gossip-based failure detection and membership protocol.

    Each node periodically:
    1. Increments its own heartbeat counter
    2. Sends its membership list to a random subset of peers
    3. Merges received membership lists (taking max heartbeat per node)
    4. Marks nodes as suspected/down if heartbeat hasn't increased

    Covers: FR-6.1, FR-6.2, FR-6.3, FR-6.4, FR-6.5, FR-6.6
    """

    def __init__(
        self,
        node_id: str,
        address: str,
        config: "ClusterConfig",
        grpc_client: "GRPCClient",
    ):
        """Initialize the gossip protocol.

        Args:
            node_id: This node's identifier.
            address: This node's address (host:port).
            config: Cluster configuration.
            grpc_client: gRPC client for sending gossip messages.
        """
        ...

    async def start(self) -> None:
        """Start the gossip protocol background task.

        Begins periodic heartbeat increment and gossip dissemination.
        """
        ...

    async def stop(self) -> None:
        """Stop the gossip protocol background task."""
        ...

    async def _gossip_round(self) -> None:
        """Execute a single gossip round.

        1. Increment own heartbeat counter
        2. Select random peers (fanout count)
        3. Send membership list to selected peers
        4. Check for suspected/down nodes based on timeout
        """
        ...

    def merge_membership(self, remote_members: dict[str, MemberInfo]) -> None:
        """Merge a received membership list with the local list.

        For each node, keep the higher heartbeat counter.
        New nodes are added; existing nodes are updated.

        Args:
            remote_members: Membership list received from a peer.
        """
        ...

    def get_alive_nodes(self) -> list[MemberInfo]:
        """Get all nodes currently considered alive.

        Returns:
            List of MemberInfo for alive nodes.
        """
        ...

    def get_node_status(self, node_id: str) -> NodeStatus:
        """Get the current status of a specific node.

        Args:
            node_id: The node to check.

        Returns:
            The node's current status.
        """
        ...

    def add_seed_node(self, node_id: str, address: str) -> None:
        """Add a seed node for initial cluster discovery.

        Args:
            node_id: Seed node identifier.
            address: Seed node address (host:port).
        """
        ...

    @property
    def members(self) -> dict[str, MemberInfo]:
        """Current membership list."""
        ...
```

### 12. Cluster — Membership

```python
from typing import Optional


class ClusterMembership:
    """Manages cluster membership state and node discovery.

    Wraps the gossip protocol and consistent hash ring to provide
    a unified view of cluster topology. Handles node join/leave events
    and updates the hash ring accordingly.

    Covers: FR-12.1, FR-12.3, FR-12.4
    """

    def __init__(
        self,
        node_id: str,
        hash_ring: "ConsistentHashRing",
        gossip: "GossipProtocol",
        config: "ClusterConfig",
    ):
        """Initialize cluster membership.

        Args:
            node_id: This node's identifier.
            hash_ring: The consistent hash ring.
            gossip: The gossip protocol instance.
            config: Cluster configuration.
        """
        ...

    async def join_cluster(self, seed_nodes: list[str]) -> None:
        """Join the cluster by contacting seed nodes.

        1. Add self to the hash ring
        2. Contact seed nodes to get current membership
        3. Start gossip protocol

        Args:
            seed_nodes: List of seed node addresses to contact.
        """
        ...

    async def leave_cluster(self) -> None:
        """Gracefully leave the cluster.

        1. Notify peers of departure
        2. Remove self from hash ring
        3. Stop gossip protocol
        """
        ...

    def is_node_alive(self, node_id: str) -> bool:
        """Check if a node is currently alive.

        Args:
            node_id: The node to check.

        Returns:
            True if the node is alive.
        """
        ...

    def get_node_address(self, node_id: str) -> Optional[str]:
        """Get the gRPC address for a node.

        Args:
            node_id: The node identifier.

        Returns:
            The node's address (host:port) or None if unknown.
        """
        ...

    def get_alive_members(self) -> list[str]:
        """Get list of all alive node IDs."""
        ...

    def on_node_joined(self, node_id: str, address: str) -> None:
        """Handle a new node joining — add to hash ring.

        Args:
            node_id: The joining node's ID.
            address: The joining node's address.
        """
        ...

    def on_node_failed(self, node_id: str) -> None:
        """Handle a node failure — remove from hash ring.

        Args:
            node_id: The failed node's ID.
        """
        ...
```

### 13. Cluster — Hinted Handoff

```python
import asyncio
from dataclasses import dataclass
from typing import Optional


@dataclass
class HintedData:
    """Data stored on behalf of an unavailable node."""
    target_node_id: str   # intended recipient
    key: str
    value: Optional[bytes]
    vector_clock: "VectorClock"
    timestamp: float
    is_tombstone: bool


class HintedHandoffManager:
    """Manages hinted handoff for temporarily unavailable nodes.

    When a write targets an unavailable node, the data is stored locally
    with a hint. A background task periodically attempts to deliver
    hinted data back to recovered nodes.

    Covers: FR-7.1, FR-7.2, FR-7.3, FR-7.4, FR-7.5
    """

    def __init__(
        self,
        node_id: str,
        config: "ClusterConfig",
        grpc_client: "GRPCClient",
        membership: "ClusterMembership",
    ):
        """Initialize the hinted handoff manager.

        Args:
            node_id: This node's identifier.
            config: Cluster configuration.
            grpc_client: gRPC client for delivering hints.
            membership: Cluster membership for checking node status.
        """
        ...

    async def start(self) -> None:
        """Start the background handoff delivery task."""
        ...

    async def stop(self) -> None:
        """Stop the background handoff delivery task."""
        ...

    async def store_hint(self, hint: HintedData) -> None:
        """Store data intended for an unavailable node.

        Args:
            hint: The hinted data to store locally.
        """
        ...

    async def deliver_hints(self, target_node_id: str) -> int:
        """Attempt to deliver all hints for a recovered node.

        Args:
            target_node_id: The node that has recovered.

        Returns:
            Number of hints successfully delivered.
        """
        ...

    async def _handoff_loop(self) -> None:
        """Background loop that periodically attempts hint delivery.

        Checks which target nodes are now alive and delivers their hints.
        Successfully delivered hints are deleted.
        """
        ...

    def get_pending_hints(self, target_node_id: Optional[str] = None) -> list[HintedData]:
        """Get pending hints, optionally filtered by target node.

        Args:
            target_node_id: Filter by target node (None for all).

        Returns:
            List of pending hinted data.
        """
        ...

    @property
    def pending_count(self) -> int:
        """Total number of pending hints."""
        ...
```

### 14. Cluster — Merkle Tree (Anti-Entropy)

```python
import hashlib
from dataclasses import dataclass
from typing import Optional


@dataclass
class MerkleNode:
    """A node in the Merkle tree."""
    hash_value: str
    left: Optional["MerkleNode"] = None
    right: Optional["MerkleNode"] = None
    bucket_id: Optional[int] = None  # leaf nodes only


@dataclass
class SyncDiff:
    """Differences found between two Merkle trees."""
    differing_buckets: list[int]
    keys_to_sync: list[str]


class MerkleTree:
    """Merkle tree for anti-entropy synchronization between replicas.

    The key space is divided into buckets. Each bucket's hash is computed
    from its contained keys. The tree enables efficient detection of
    inconsistencies — only differing subtrees need to be compared.

    Covers: FR-8.1, FR-8.2, FR-8.3, FR-8.4, FR-8.5
    """

    def __init__(self, num_buckets: int = 1024):
        """Initialize the Merkle tree with the given number of buckets.

        Args:
            num_buckets: Number of leaf buckets (must be power of 2).
        """
        ...

    def update(self, key: str, value_hash: str) -> None:
        """Update the tree when a key's value changes.

        Recomputes hashes from the affected leaf up to the root.

        Args:
            key: The key that was updated.
            value_hash: Hash of the new value.
        """
        ...

    def remove(self, key: str) -> None:
        """Remove a key from the tree.

        Args:
            key: The key to remove.
        """
        ...

    def get_root_hash(self) -> str:
        """Get the root hash of the Merkle tree.

        Returns:
            The root hash string. If two trees have the same root hash,
            they contain identical data.
        """
        ...

    def get_bucket_hash(self, bucket_id: int) -> str:
        """Get the hash for a specific bucket.

        Args:
            bucket_id: The bucket index.

        Returns:
            The hash for that bucket.
        """
        ...

    def compare(self, other_tree_hashes: dict[int, str]) -> list[int]:
        """Compare this tree with another tree's bucket hashes.

        Efficiently finds differing buckets by comparing from root down.

        Args:
            other_tree_hashes: Mapping of bucket_id → hash from the remote tree.

        Returns:
            List of bucket IDs that differ.
        """
        ...

    def get_keys_in_bucket(self, bucket_id: int) -> list[str]:
        """Get all keys assigned to a specific bucket.

        Args:
            bucket_id: The bucket index.

        Returns:
            List of keys in that bucket.
        """
        ...

    def _key_to_bucket(self, key: str) -> int:
        """Map a key to its bucket index.

        Args:
            key: The key to map.

        Returns:
            Bucket index (0 to num_buckets-1).
        """
        ...

    def rebuild(self) -> None:
        """Rebuild the entire tree from current bucket state."""
        ...


class AntiEntropyManager:
    """Manages periodic anti-entropy synchronization with replica nodes.

    Periodically compares Merkle tree roots with replicas. When differences
    are found, synchronizes only the differing buckets.

    Covers: FR-8.3, FR-8.4, FR-8.5
    """

    def __init__(
        self,
        node_id: str,
        merkle_tree: MerkleTree,
        config: "ClusterConfig",
        grpc_client: "GRPCClient",
        storage_engine: "StorageEngine",
        hash_ring: "ConsistentHashRing",
    ):
        """Initialize the anti-entropy manager.

        Args:
            node_id: This node's identifier.
            merkle_tree: The local Merkle tree.
            config: Cluster configuration.
            grpc_client: gRPC client for sync communication.
            storage_engine: Local storage for reading/writing keys.
            hash_ring: Hash ring for determining replica peers.
        """
        ...

    async def start(self) -> None:
        """Start the periodic anti-entropy background task."""
        ...

    async def stop(self) -> None:
        """Stop the anti-entropy background task."""
        ...

    async def sync_with_peer(self, peer_node_id: str) -> SyncDiff:
        """Synchronize with a specific peer node.

        1. Exchange Merkle tree root hashes
        2. If roots differ, compare bucket hashes
        3. For differing buckets, exchange and reconcile keys

        Args:
            peer_node_id: The peer to sync with.

        Returns:
            SyncDiff describing what was synchronized.
        """
        ...
```

### 15. Network — gRPC Service Definition (Protobuf)

```protobuf
syntax = "proto3";

package kvstore;

// The KVStore service defines all inter-node and client-node RPCs.
service KVStoreService {
    // Client-facing operations
    rpc Put(PutRequest) returns (PutResponse);
    rpc Get(GetRequest) returns (GetResponse);
    rpc Delete(DeleteRequest) returns (DeleteResponse);

    // Inter-node replication
    rpc Replicate(ReplicateRequest) returns (ReplicateResponse);

    // Gossip protocol
    rpc GossipExchange(GossipMessage) returns (GossipMessage);

    // Hinted handoff delivery
    rpc HintedHandoff(HintedHandoffRequest) returns (HintedHandoffResponse);

    // Anti-entropy Merkle tree sync
    rpc MerkleTreeSync(MerkleTreeSyncRequest) returns (MerkleTreeSyncResponse);
}

message VectorClockEntry {
    string node_id = 1;
    int64 counter = 2;
}

message VectorClockProto {
    repeated VectorClockEntry entries = 1;
}

message PutRequest {
    string key = 1;
    bytes value = 2;
    VectorClockProto vector_clock = 3;  // optional: for read-modify-write
    string consistency_level = 4;        // "one", "quorum", "all"
}

message PutResponse {
    bool success = 1;
    VectorClockProto vector_clock = 2;
    string error_message = 3;
}

message GetRequest {
    string key = 1;
    string consistency_level = 2;
}

message GetResponse {
    bool found = 1;
    bytes value = 2;
    VectorClockProto vector_clock = 3;
    bool has_conflict = 4;
    repeated ConflictingValue conflicting_values = 5;
    string error_message = 6;
}

message ConflictingValue {
    bytes value = 1;
    VectorClockProto vector_clock = 2;
}

message DeleteRequest {
    string key = 1;
    VectorClockProto vector_clock = 2;
    string consistency_level = 3;
}

message DeleteResponse {
    bool success = 1;
    VectorClockProto vector_clock = 2;
    string error_message = 3;
}

message ReplicateRequest {
    string key = 1;
    bytes value = 2;
    VectorClockProto vector_clock = 3;
    bool is_tombstone = 4;
    double timestamp = 5;
}

message ReplicateResponse {
    bool success = 1;
    string error_message = 2;
}

message GossipMessage {
    string sender_id = 1;
    repeated MemberInfoProto members = 2;
}

message MemberInfoProto {
    string node_id = 1;
    string address = 2;
    int64 heartbeat_counter = 3;
    string status = 4;
}

message HintedHandoffRequest {
    string target_node_id = 1;
    string key = 2;
    bytes value = 3;
    VectorClockProto vector_clock = 4;
    bool is_tombstone = 5;
    double timestamp = 6;
}

message HintedHandoffResponse {
    bool success = 1;
    string error_message = 2;
}

message MerkleTreeSyncRequest {
    string sender_id = 1;
    map<int32, string> bucket_hashes = 2;  // bucket_id → hash
    repeated string keys_to_send = 3;       // keys for differing buckets
    repeated KeyValuePair key_values = 4;   // actual data for sync
}

message MerkleTreeSyncResponse {
    repeated int32 differing_buckets = 1;
    repeated KeyValuePair key_values = 2;   // data from this node for sync
    bool success = 3;
}

message KeyValuePair {
    string key = 1;
    bytes value = 2;
    VectorClockProto vector_clock = 3;
    bool is_tombstone = 4;
    double timestamp = 5;
}
```

### 16. Network — gRPC Server

```python
import asyncio
from typing import Optional


class GRPCServer:
    """Async gRPC server handling all inter-node and client RPCs.

    Each node runs one gRPC server that handles:
    - Client operations (Put, Get, Delete)
    - Replication requests from coordinator nodes
    - Gossip protocol message exchange
    - Hinted handoff delivery
    - Merkle tree anti-entropy sync

    Covers: FR-11.1, FR-11.2, FR-11.3
    """

    def __init__(
        self,
        config: "NetworkConfig",
        coordinator: "RequestCoordinator",
        storage_engine: "StorageEngine",
        gossip: "GossipProtocol",
        hinted_handoff: "HintedHandoffManager",
        merkle_tree: "MerkleTree",
    ):
        """Initialize the gRPC server.

        Args:
            config: Network configuration (host, port, etc.).
            coordinator: Request coordinator for client operations.
            storage_engine: Local storage for replication writes.
            gossip: Gossip protocol for membership exchange.
            hinted_handoff: Hinted handoff for receiving hints.
            merkle_tree: Merkle tree for anti-entropy sync.
        """
        ...

    async def start(self) -> None:
        """Start the gRPC server and begin accepting connections."""
        ...

    async def stop(self) -> None:
        """Gracefully stop the gRPC server."""
        ...

    async def Put(self, request: "PutRequest") -> "PutResponse":
        """Handle a Put RPC — delegates to coordinator."""
        ...

    async def Get(self, request: "GetRequest") -> "GetResponse":
        """Handle a Get RPC — delegates to coordinator."""
        ...

    async def Delete(self, request: "DeleteRequest") -> "DeleteResponse":
        """Handle a Delete RPC — delegates to coordinator."""
        ...

    async def Replicate(self, request: "ReplicateRequest") -> "ReplicateResponse":
        """Handle a Replicate RPC — writes directly to local storage."""
        ...

    async def GossipExchange(self, request: "GossipMessage") -> "GossipMessage":
        """Handle gossip exchange — merge membership and return local state."""
        ...

    async def HintedHandoff(self, request: "HintedHandoffRequest") -> "HintedHandoffResponse":
        """Handle hinted handoff delivery — write to local storage."""
        ...

    async def MerkleTreeSync(self, request: "MerkleTreeSyncRequest") -> "MerkleTreeSyncResponse":
        """Handle Merkle tree sync — compare and return differences."""
        ...
```

### 17. Network — gRPC Client

```python
import asyncio
from typing import Optional


class GRPCClient:
    """Async gRPC client for inter-node communication.

    Maintains connection pools to peer nodes and provides typed methods
    for all inter-node RPCs. Handles connection failures gracefully.

    Covers: FR-11.4
    """

    def __init__(self, config: "NetworkConfig"):
        """Initialize the gRPC client.

        Args:
            config: Network configuration.
        """
        ...

    async def put(
        self, target: str, key: str, value: bytes, vector_clock: "VectorClock"
    ) -> "PutResponse":
        """Send a Put request to a target node.

        Args:
            target: Target node address (host:port).
            key: Key to write.
            value: Value bytes.
            vector_clock: Vector clock for the write.

        Returns:
            PutResponse from the target node.

        Raises:
            NodeUnavailableError: If the target node is unreachable.
        """
        ...

    async def get(self, target: str, key: str) -> "GetResponse":
        """Send a Get request to a target node.

        Args:
            target: Target node address (host:port).
            key: Key to read.

        Returns:
            GetResponse from the target node.

        Raises:
            NodeUnavailableError: If the target node is unreachable.
        """
        ...

    async def replicate(
        self,
        target: str,
        key: str,
        value: Optional[bytes],
        vector_clock: "VectorClock",
        is_tombstone: bool,
        timestamp: float,
    ) -> "ReplicateResponse":
        """Send a Replicate request to a target node.

        Args:
            target: Target node address.
            key: Key being replicated.
            value: Value bytes (None for tombstones).
            vector_clock: Vector clock.
            is_tombstone: Whether this is a delete tombstone.
            timestamp: Write timestamp.

        Returns:
            ReplicateResponse from the target.

        Raises:
            NodeUnavailableError: If the target is unreachable.
        """
        ...

    async def gossip_exchange(
        self, target: str, members: dict[str, "MemberInfo"]
    ) -> dict[str, "MemberInfo"]:
        """Exchange gossip membership lists with a peer.

        Args:
            target: Peer node address.
            members: Local membership list to send.

        Returns:
            Remote membership list received from peer.

        Raises:
            NodeUnavailableError: If the peer is unreachable.
        """
        ...

    async def send_hinted_handoff(
        self, target: str, hint: "HintedData"
    ) -> bool:
        """Deliver hinted data to a recovered node.

        Args:
            target: Target node address.
            hint: The hinted data to deliver.

        Returns:
            True if delivery was acknowledged.

        Raises:
            NodeUnavailableError: If the target is unreachable.
        """
        ...

    async def merkle_tree_sync(
        self, target: str, bucket_hashes: dict[int, str]
    ) -> "MerkleTreeSyncResponse":
        """Exchange Merkle tree hashes with a peer for anti-entropy.

        Args:
            target: Peer node address.
            bucket_hashes: Local bucket hashes to compare.

        Returns:
            MerkleTreeSyncResponse with differences and data.

        Raises:
            NodeUnavailableError: If the peer is unreachable.
        """
        ...

    async def close(self) -> None:
        """Close all connections."""
        ...
```

### 18. Node Orchestrator

```python
import asyncio
from typing import Optional


class KVNode:
    """Main node orchestrator — ties all components together.

    Each node is a self-contained unit with identical responsibilities.
    The node manages its lifecycle: startup, joining the cluster,
    serving requests, and graceful shutdown.

    Covers: FR-12.1, FR-12.2
    """

    def __init__(self, config: "NodeConfig"):
        """Initialize the KV node with all sub-components.

        Args:
            config: Complete node configuration.
        """
        self.config = config
        self.node_id = config.node_id

        # Storage layer
        self.storage_engine: StorageEngine = StorageEngine(config.storage)

        # Partitioning (reuses consistent-hashing library)
        self.hash_ring: ConsistentHashRing = ConsistentHashRing(
            num_virtual_nodes=config.virtual_nodes
        )

        # Network layer
        self.grpc_client: GRPCClient = GRPCClient(config.network)

        # Cluster layer
        self.gossip: GossipProtocol = GossipProtocol(
            node_id=config.node_id,
            address=f"{config.network.host}:{config.network.port}",
            config=config.cluster,
            grpc_client=self.grpc_client,
        )
        self.membership: ClusterMembership = ClusterMembership(
            node_id=config.node_id,
            hash_ring=self.hash_ring,
            gossip=self.gossip,
            config=config.cluster,
        )
        self.hinted_handoff: HintedHandoffManager = HintedHandoffManager(
            node_id=config.node_id,
            config=config.cluster,
            grpc_client=self.grpc_client,
            membership=self.membership,
        )
        self.merkle_tree: MerkleTree = MerkleTree(
            num_buckets=config.cluster.merkle_tree_buckets
        )
        self.anti_entropy: AntiEntropyManager = AntiEntropyManager(
            node_id=config.node_id,
            merkle_tree=self.merkle_tree,
            config=config.cluster,
            grpc_client=self.grpc_client,
            storage_engine=self.storage_engine,
            hash_ring=self.hash_ring,
        )

        # Replication layer
        self.quorum_manager: QuorumManager = QuorumManager(config.replication)
        self.coordinator: RequestCoordinator = RequestCoordinator(
            node_id=config.node_id,
            hash_ring=self.hash_ring,
            quorum_manager=self.quorum_manager,
            grpc_client=self.grpc_client,
            storage_engine=self.storage_engine,
            membership=self.membership,
            hinted_handoff=self.hinted_handoff,
        )

        # gRPC server
        self.grpc_server: GRPCServer = GRPCServer(
            config=config.network,
            coordinator=self.coordinator,
            storage_engine=self.storage_engine,
            gossip=self.gossip,
            hinted_handoff=self.hinted_handoff,
            merkle_tree=self.merkle_tree,
        )

    async def start(self) -> None:
        """Start the node and all sub-components.

        Startup order:
        1. Storage engine (replay WAL, load SSTables)
        2. gRPC server (begin accepting connections)
        3. Join cluster (contact seed nodes, start gossip)
        4. Start background tasks (hinted handoff, anti-entropy)
        """
        await self.storage_engine.start()
        await self.grpc_server.start()
        await self.membership.join_cluster(self.config.cluster.seed_nodes)
        await self.hinted_handoff.start()
        await self.anti_entropy.start()

    async def stop(self) -> None:
        """Gracefully stop the node.

        Shutdown order:
        1. Stop background tasks
        2. Leave cluster (notify peers)
        3. Stop gRPC server
        4. Flush and close storage engine
        """
        await self.anti_entropy.stop()
        await self.hinted_handoff.stop()
        await self.membership.leave_cluster()
        await self.grpc_server.stop()
        await self.storage_engine.stop()
        await self.grpc_client.close()

    async def put(self, key: str, value: bytes, **kwargs) -> "PutResult":
        """Put a key-value pair (delegates to coordinator)."""
        return await self.coordinator.put(key, value, **kwargs)

    async def get(self, key: str, **kwargs) -> "GetResult":
        """Get a value by key (delegates to coordinator)."""
        return await self.coordinator.get(key, **kwargs)

    async def delete(self, key: str, **kwargs) -> "DeleteResult":
        """Delete a key (delegates to coordinator)."""
        return await self.coordinator.delete(key, **kwargs)
```

### 19. Client API

```python
import asyncio
from dataclasses import dataclass
from typing import Optional


@dataclass
class ClientConfig:
    """Client configuration."""
    seed_nodes: list[str]                    # list of node addresses to connect to
    default_consistency: str = "quorum"      # "one", "quorum", "all"
    timeout_seconds: float = 5.0
    retry_count: int = 3
    retry_delay_seconds: float = 0.5


@dataclass
class KVResponse:
    """Response from a KV operation."""
    success: bool
    value: Optional[bytes] = None
    vector_clock: Optional[dict[str, int]] = None
    has_conflict: bool = False
    conflicting_values: list[tuple[bytes, dict[str, int]]] = None
    error: Optional[str] = None


class KVClient:
    """Client for the distributed key-value store.

    Connects to any node in the cluster (decentralized — any node can
    serve as coordinator). Supports automatic retry and failover to
    other nodes if the connected node is unavailable.

    Covers: FR-1.1, FR-1.2, FR-1.3, FR-1.4, FR-1.5
    """

    def __init__(self, config: ClientConfig):
        """Initialize the KV client.

        Args:
            config: Client configuration with seed nodes.
        """
        ...

    async def connect(self) -> None:
        """Establish connection to a cluster node.

        Tries seed nodes in order until one responds.

        Raises:
            ConnectionError: If no seed nodes are reachable.
        """
        ...

    async def put(
        self,
        key: str,
        value: bytes,
        vector_clock: Optional[dict[str, int]] = None,
        consistency: Optional[str] = None,
    ) -> KVResponse:
        """Put a key-value pair into the store.

        Args:
            key: Key string (max 256 bytes).
            value: Value bytes (max 10 KB).
            vector_clock: Optional vector clock for read-modify-write pattern.
            consistency: Optional consistency level override.

        Returns:
            KVResponse with success status and updated vector clock.

        Raises:
            ValueError: If key or value exceeds size limits.
            ConnectionError: If no nodes are reachable after retries.
        """
        ...

    async def get(
        self,
        key: str,
        consistency: Optional[str] = None,
    ) -> KVResponse:
        """Get a value by key.

        If a conflict exists (concurrent writes detected via vector clocks),
        the response will contain all conflicting values for client-side
        resolution.

        Args:
            key: Key to retrieve.
            consistency: Optional consistency level override.

        Returns:
            KVResponse with value(s) and vector clock metadata.
        """
        ...

    async def delete(
        self,
        key: str,
        vector_clock: Optional[dict[str, int]] = None,
        consistency: Optional[str] = None,
    ) -> KVResponse:
        """Delete a key from the store.

        Uses tombstone markers internally. The key will be physically
        removed during SSTable compaction.

        Args:
            key: Key to delete.
            vector_clock: Optional vector clock for consistency.
            consistency: Optional consistency level override.

        Returns:
            KVResponse with success status.
        """
        ...

    async def close(self) -> None:
        """Close the client connection."""
        ...

    async def __aenter__(self) -> "KVClient":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, *args) -> None:
        """Async context manager exit."""
        await self.close()
```

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Language & Runtime | Python >= 3.9, asyncio | Async I/O for concurrent network operations; modern Python features (type hints, dataclasses) |
| Build System | hatchling (pyproject.toml) | Modern Python packaging standard, consistent with other projects in repo |
| Project Layout | src layout | Prevents accidental imports from project root; packaging best practice |
| Partitioning | Reuse `consistent-hashing` library | Avoid reimplementation; proven ConsistentHashRing with virtual nodes and `get_nodes()` for replication |
| Inter-node Communication | gRPC + Protobuf | Efficient binary serialization, strong typing, bidirectional streaming support, code generation |
| Storage Engine | LSM-tree (WAL → MemTable → SSTable) | Write-optimized; sequential disk writes; proven pattern (LevelDB, RocksDB, Cassandra) |
| Conflict Resolution | Vector clocks | Captures causal ordering without centralized coordination; detects concurrent writes |
| Failure Detection | Gossip protocol | Decentralized, scalable, eventually consistent membership — no SPOF |
| Temporary Failures | Sloppy quorum + hinted handoff | Maintains availability during transient failures; data is not lost |
| Permanent Failure Repair | Merkle tree anti-entropy | Efficient inconsistency detection — only sync differing data ranges |
| Read Optimization | Bloom filters per SSTable | Avoids unnecessary disk reads; O(1) negative lookups with configurable FP rate |
| Consistency Model | Tunable N/W/R quorum | Flexibility: strong consistency (W+R>N) or eventual consistency (W=1,R=1) per operation |
| Compaction Strategy | Size-tiered | Simpler implementation; good write amplification characteristics for write-heavy workloads |
| Concurrency Model | asyncio (single-threaded event loop) | Avoids GIL contention; natural fit for I/O-bound distributed system operations |
| Data Size Limits | Key ≤ 256B, Value ≤ 10KB | Prevents memory pressure; keeps MemTable and network messages bounded |
| Vector Clock Pruning | Max 10 entries, prune oldest | Prevents unbounded growth; acceptable trade-off for large clusters |

## Error Handling

| Error Scenario | Handling Strategy |
|----------------|-------------------|
| **Quorum not met** | Raise `QuorumNotMetError` to client with details on which nodes failed. Client can retry with lower consistency. |
| **Node unreachable (transient)** | Sloppy quorum: route to next healthy node on ring. Store hint for later delivery. Log warning. |
| **Node permanently failed** | Gossip marks node as DOWN. Anti-entropy detects and repairs missing replicas on remaining nodes. |
| **WAL write failure** | Raise `IOError` — write is not acknowledged. MemTable is not updated. Client receives failure. |
| **MemTable flush failure** | Retry flush. If persistent, mark node as degraded. WAL preserves data for recovery. |
| **SSTable corruption** | Detect via checksum validation on read. Mark SSTable as corrupt. Repair from replicas via anti-entropy. |
| **Key/value size exceeded** | Raise `ValueError` immediately at client and coordinator level before any writes. |
| **Vector clock conflict** | Return all conflicting versions to client (FR-5.5). Client resolves and writes back with merged clock. |
| **Gossip message lost** | Protocol is eventually consistent — next gossip round will propagate the information. No explicit retry needed. |
| **Hinted handoff delivery failure** | Retry on next handoff interval. Hints are persisted locally until successful delivery. |
| **Merkle tree sync failure** | Log error, retry on next anti-entropy interval. Data remains available via other replicas. |
| **gRPC connection timeout** | Default 5s timeout. Mark node as potentially failed. Gossip will confirm status. |
| **Startup WAL replay failure** | Log error with details. Node refuses to start in inconsistent state. Operator intervention required. |
