"""Configuration dataclasses for the distributed key-value store.

Provides typed, default-valued configuration for all node subsystems:
storage engine, replication, cluster membership, and networking.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StorageConfig:
    """LSM-tree storage engine configuration."""

    data_dir: str = "./data"
    wal_dir: str = "./data/wal"
    sstable_dir: str = "./data/sstables"
    memtable_size_bytes: int = 4 * 1024 * 1024  # 4 MB flush threshold
    bloom_filter_fp_rate: float = 0.01  # 1% false positive rate
    compaction_threshold: int = 4  # merge after N SSTables


@dataclass
class ReplicationConfig:
    """Quorum and replication configuration."""

    n_replicas: int = 3  # total replicas per key
    w_quorum: int = 2  # write quorum
    r_quorum: int = 2  # read quorum
    vector_clock_max_entries: int = 10  # prune oldest entries beyond this


@dataclass
class ClusterConfig:
    """Cluster membership and protocol configuration."""

    gossip_interval_seconds: float = 1.0
    gossip_fanout: int = 3  # number of peers per gossip round
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
