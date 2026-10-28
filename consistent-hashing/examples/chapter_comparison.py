"""Reproducible chapter experiments; outputs measurements, not guarantees."""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from consistent_hashing import (
    ConsistentHashRing,
    affected_ranges,
    iter_migrations,
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
    selected = list(iter_migrations(before, after, iter(keys)))
    assert len(selected) == changed
    assert all(item.new_owner == "server-10" for item in selected)
    before_remove = ring.snapshot()
    ring.remove_node("server-1")
    removed = list(iter_migrations(before_remove, ring.snapshot(), iter(keys)))
    assert all(item.previous_owner == "server-1" for item in removed)
    before_weight = ring.snapshot()
    ring.set_virtual_nodes("server-0", 400)
    weighted = list(iter_migrations(before_weight, ring.snapshot(), iter(keys)))
    assert all(item.new_owner == "server-0" for item in weighted)
    ring.set_virtual_nodes("server-0", 200)
    assert list(ring.iter_assignments(keys)) == list(
        before_weight.iter_assignments(keys)
    )
    print(
        json.dumps(
            {
                "sample_keys": len(keys),
                "distribution": distributions,
                "add_server": {
                    "consistent_moved": changed,
                    "modulo_moved": modulo_changed,
                    "affected_intervals": len(ranges),
                    "selected_keys": len(selected),
                },
                "remove_server": {
                    "moved": len(removed),
                    "only_removed_owner_moves": True,
                },
                "weight_change": {
                    "from": 200,
                    "to": 400,
                    "moved": len(weighted),
                    "restored_routes": True,
                },
                "keys_for_server_0": len(ring.get_keys("server-0", iter(keys))),
                "limits": "Uniform key sample, not traffic. Virtual nodes do not split a single hot key. No data transfer performed.",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
