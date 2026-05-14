"""Distribution statistics and redistribution analysis utilities.

Provides tools for analyzing key distribution across servers on a
consistent hash ring, including per-server counts, standard deviation,
and redistribution tracking when topology changes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from consistent_hashing.ring import ConsistentHashRing


@dataclass
class DistributionStats:
    """Statistics about key distribution across servers.

    Attributes:
        total_keys: Total number of keys analyzed.
        num_servers: Number of physical servers on the ring.
        keys_per_server: Mapping of server name to number of keys assigned.
        mean: Mean number of keys per server.
        std_dev: Standard deviation of keys per server.
        min_keys: Minimum keys assigned to any single server.
        max_keys: Maximum keys assigned to any single server.
        balance_ratio: Ratio of std_dev to mean (lower is better).
    """

    total_keys: int
    num_servers: int
    keys_per_server: dict[str, int]
    mean: float
    std_dev: float
    min_keys: int
    max_keys: int
    balance_ratio: float


def compute_redistribution(
    keys: list[str],
    assignment_before: dict[str, str],
    assignment_after: dict[str, str],
) -> dict[str, object]:
    """Compute which keys moved between two assignments.

    Args:
        keys: All keys.
        assignment_before: {key: server} mapping before change.
        assignment_after: {key: server} mapping after change.

    Returns:
        Dictionary with:
            "moved": {key: {"from": old_server, "to": new_server}} for each moved key.
            "total_moved": int count of keys that changed server.
    """
    moved: dict[str, dict[str, str]] = {}

    for key in keys:
        old_server = assignment_before.get(key)
        new_server = assignment_after.get(key)

        if old_server is not None and new_server is not None and old_server != new_server:
            moved[key] = {"from": old_server, "to": new_server}

    return {"moved": moved, "total_moved": len(moved)}


def compute_distribution(ring: "ConsistentHashRing", keys: list[str]) -> DistributionStats:
    """Compute distribution statistics for a set of keys on the ring.

    Args:
        ring: The consistent hash ring.
        keys: List of keys to analyze.

    Returns:
        DistributionStats with per-server counts and aggregate metrics.

    Raises:
        RuntimeError: If the ring is empty.
    """
    # Count keys per server
    keys_per_server: dict[str, int] = {node: 0 for node in ring.nodes}

    for key in keys:
        node = ring.get_node(key)
        keys_per_server[node] = keys_per_server.get(node, 0) + 1

    total_keys = len(keys)
    num_servers = len(keys_per_server)

    if num_servers == 0:
        raise RuntimeError("Hash ring is empty - no nodes available.")

    counts = list(keys_per_server.values())
    mean = total_keys / num_servers

    # Standard deviation
    variance = sum((c - mean) ** 2 for c in counts) / num_servers
    std_dev = math.sqrt(variance)

    min_keys = min(counts)
    max_keys = max(counts)
    balance_ratio = std_dev / mean if mean > 0 else 0.0

    return DistributionStats(
        total_keys=total_keys,
        num_servers=num_servers,
        keys_per_server=keys_per_server,
        mean=mean,
        std_dev=std_dev,
        min_keys=min_keys,
        max_keys=max_keys,
        balance_ratio=balance_ratio,
    )
