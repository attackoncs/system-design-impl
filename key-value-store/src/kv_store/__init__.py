"""Distributed key-value store with tunable consistency.

A Dynamo-inspired distributed key-value store featuring consistent hashing,
vector clocks, quorum-based replication, gossip protocol membership,
and LSM-tree storage.
"""

from kv_store.config import (
    ClusterConfig,
    NetworkConfig,
    NodeConfig,
    ReplicationConfig,
    StorageConfig,
)
from kv_store.node import KVNode

__version__ = "0.1.0"

__all__ = [
    "KVNode",
    "NodeConfig",
    "StorageConfig",
    "ReplicationConfig",
    "ClusterConfig",
    "NetworkConfig",
    "__version__",
]
