"""Reproducible chapter experiments; outputs measurements, not guarantees."""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from consistent_hashing import (
    ConsistentHashRing,
    affected_ranges,
    compute_distribution,
    sha1_hash,
)


def main():
    keys = [f"key-{index}" for index in range(10000)]
    nodes = [f"server-{index}" for index in range(10)]
    distributions = []
    for count in [1, 100, 200]:
        ring = ConsistentHashRing(nodes, num_virtual_nodes=count)
        stats = compute_distribution(ring, keys)
        distributions.append(
            {
                "virtual_nodes_per_server": count,
                "ring_entries": ring.total_virtual_nodes,
                "std_dev_over_mean": stats.balance_ratio,
            }
        )
    before = ring.snapshot()
    ring.add_node("server-10")
    after = ring.snapshot()
    changed = sum(before.get_node(key) != after.get_node(key) for key in keys)
    modulo_changed = sum(sha1_hash(key) % 10 != sha1_hash(key) % 11 for key in keys)
    ranges = affected_ranges(before, after)
    print(
        json.dumps(
            {
                "sample_keys": len(keys),
                "distribution": distributions,
                "add_server": {
                    "consistent_moved": changed,
                    "modulo_moved": modulo_changed,
                    "affected_intervals": len(ranges),
                },
                "limits": "Uniform key sample, not traffic. Virtual nodes do not split a single hot key. No data transfer performed.",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
