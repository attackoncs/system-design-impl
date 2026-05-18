"""Cluster layer package for distributed membership and synchronization.

Provides gossip-based membership, failure detection, hinted handoff for
temporary failures, and Merkle tree-based anti-entropy for replica sync.
"""

from kv_store.cluster.gossip import GossipProtocol, MemberInfo, NodeStatus
from kv_store.cluster.membership import ClusterMembership
from kv_store.cluster.hinted_handoff import HintedHandoffManager
from kv_store.cluster.merkle_tree import AntiEntropyManager, MerkleTree

__all__ = [
    "GossipProtocol",
    "NodeStatus",
    "MemberInfo",
    "ClusterMembership",
    "HintedHandoffManager",
    "MerkleTree",
    "AntiEntropyManager",
]
