"""Consistent Hashing library with virtual nodes for balanced key distribution.

This package implements the consistent hash ring algorithm, enabling distributed
systems to map keys to servers with minimal redistribution when the server
topology changes. It supports configurable hash functions, virtual nodes for
balanced distribution, and statistics utilities for analyzing key placement.

Usage:
    from consistent_hashing import ConsistentHashRing, sha1_hash

    ring = ConsistentHashRing(nodes=["server-1", "server-2", "server-3"])
    server = ring.get_node("my-key")
"""

from consistent_hashing.hash_functions import (
    HashFunction,
    md5_hash,
    sha1_hash,
    sha256_hash,
)
from consistent_hashing.ring import ConsistentHashRing
from consistent_hashing.stats import (
    DistributionStats,
    compute_distribution,
    compute_redistribution,
)

__all__ = [
    "ConsistentHashRing",
    "HashFunction",
    "sha1_hash",
    "md5_hash",
    "sha256_hash",
    "DistributionStats",
    "compute_distribution",
    "compute_redistribution",
]
