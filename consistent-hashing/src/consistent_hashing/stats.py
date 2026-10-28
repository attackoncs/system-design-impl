"""Distribution statistics and redistribution analysis utilities.

Provides tools for analyzing key distribution across servers on a consistent
hash ring, including per-server counts, standard deviation, and redistribution
tracking when topology changes.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from consistent_hashing.ring import ConsistentHashRing, RingSnapshot


@dataclasses.dataclass
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
    keys: Iterable[str],
    assignment_before: Mapping[str, str | None],
    assignment_after: Mapping[str, str | None],
    *,
    strict: bool = False,
) -> dict[str, object]:
    """Compute which keys moved between two assignments.

    Args:
        keys: All keys.
        assignment_before: {key: server} mapping before change.
        assignment_after: {key: server} mapping after change.
        strict: Reject missing keys/invalid owners and count explicit None
            transitions. False retains legacy behavior of skipping missing or
            unassigned pairs. Duplicate keys contribute one moved-map entry.

    Returns:
        Dictionary with:
            "moved": {key: {"from": old_server, "to": new_server}} for each
            moved key.
            "total_moved": int count of keys that changed server.
    """
    if type(strict) is not bool:
        raise ValueError("strict must be bool")
    moved: dict[str, dict[str, str | None]] = {}

    for key in keys:
        if strict and (key not in assignment_before or key not in assignment_after):
            raise KeyError(f"missing assignment for {key!r}")
        old_server = assignment_before.get(key)
        new_server = assignment_after.get(key)
        if strict and any(
            value is not None and not isinstance(value, str)
            for value in (old_server, new_server)
        ):
            raise ValueError("assignment owners must be strings or None")

        if old_server != new_server and (
            strict or (old_server is not None and new_server is not None)
        ):
            moved[key] = {"from": old_server, "to": new_server}

    return {"moved": moved, "total_moved": len(moved)}


def compute_distribution(
    ring: "ConsistentHashRing | RingSnapshot", keys: Iterable[str]
) -> DistributionStats:
    """Compute distribution statistics for a set of keys on the ring.

    Args:
        ring: The consistent hash ring.
        keys: One-pass iterable of keys to analyze; duplicates count as samples.

    Returns:
        DistributionStats with per-server counts and aggregate metrics.

    Raises:
        RuntimeError: If the ring is empty.
    """
    # One capture prevents mixed-topology totals while nodes change.
    ring = ring.snapshot()
    keys_per_server: dict[str, int] = {node: 0 for node in ring.nodes}
    if not keys_per_server:
        raise RuntimeError("Hash ring is empty - no nodes available.")

    total_keys = 0
    for _, node in ring.iter_assignments(keys):
        keys_per_server[node] += 1
        total_keys += 1

    num_servers = len(keys_per_server)

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
