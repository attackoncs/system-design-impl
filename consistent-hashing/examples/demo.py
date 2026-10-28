#!/usr/bin/env python3
"""Demonstration of the consistent hashing library.

Shows ring creation, key assignment, server addition/removal, redistribution
analysis, and distribution statistics.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import consistent_hashing.stats
from consistent_hashing import ConsistentHashRing, compute_distribution


def print_separator(title: str) -> None:
    """Print a section separator."""
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


def print_stats(stats) -> None:
    """Print distribution statistics."""
    print(f"  Total keys: {stats.total_keys}")
    print(f"  Servers: {stats.num_servers}")
    print(f"  Mean keys/server: {stats.mean:.1f}")
    print(f"  Std deviation: {stats.std_dev:.1f}")
    print(f"  Balance ratio: {stats.balance_ratio:.4f} (lower is better)")
    print(f"  Min keys: {stats.min_keys}")
    print(f"  Max keys: {stats.max_keys}")
    print("  Keys per server:")
    for server, count in sorted(stats.keys_per_server.items()):
        bar = "#" * (count // 5)
        print(f"    {server:>12}: {count:4d} {bar}")


def main() -> None:
    """Run the consistent hashing demonstration."""
    # --- 1. Ring Creation ---
    print_separator("1. Creating Hash Ring with 3 Servers")

    servers = ["web-1", "web-2", "web-3"]
    ring = ConsistentHashRing(nodes=servers, num_virtual_nodes=150)

    print(f"  Servers: {ring.nodes}")
    print("  Virtual nodes per server: 150")
    print(f"  Total virtual nodes: {ring.total_virtual_nodes}")

    # --- 2. Key Assignment ---
    print_separator("2. Key Assignment")

    keys = [f"user:{i}" for i in range(1000)]
    sample_keys = keys[:10]

    print("  Sample key assignments:")
    for key in sample_keys:
        server = ring.get_node(key)
        print(f"    {key:>10} -> {server}")

    print("\n  Replication example (get_nodes with count=2):")
    for key in sample_keys[:3]:
        replicas = ring.get_nodes(key, count=2)
        print(f"    {key:>10} -> {replicas}")

    # --- 3. Distribution Statistics (Before) ---
    print_separator("3. Distribution Statistics (3 Servers)")

    stats_before = compute_distribution(ring, keys)
    print_stats(stats_before)

    # --- 4. Adding a Server ---
    print_separator("4. Adding Server 'web-4'")

    # Record assignments before
    assignment_before = dict(ring.iter_assignments(keys))

    # Add new server
    ring.add_node("web-4")
    print(f"  Servers after add: {ring.nodes}")
    print(f"  Total virtual nodes: {ring.total_virtual_nodes}")

    # Record assignments after
    assignment_after = dict(ring.iter_assignments(keys))

    # Compute redistribution
    redist = consistent_hashing.stats.compute_redistribution(
        keys, assignment_before, assignment_after, strict=True
    )
    moved_count = redist["total_moved"]
    print(
        f"\n  Keys redistributed: {moved_count}/{len(keys)}"
        f" ({moved_count / len(keys) * 100:.1f}%)"
    )
    print(f"  Theoretical ideal: ~{len(keys) // 4} ({100 / 4:.1f}%)")

    # Show some moved keys
    moved = redist["moved"]
    sample_moved = list(moved.items())[:5]
    if sample_moved:
        print("\n  Sample moved keys:")
        for key, info in sample_moved:
            print(f"    {key:>10}: {info['from']} -> {info['to']}")

    # --- 5. Distribution After Adding ---
    print_separator("5. Distribution Statistics (4 Servers)")

    stats_after_add = compute_distribution(ring, keys)
    print_stats(stats_after_add)

    # --- 6. Removing a Server ---
    print_separator("6. Removing Server 'web-2'")

    assignment_before_remove = dict(ring.iter_assignments(keys))

    ring.remove_node("web-2")
    print(f"  Servers after remove: {ring.nodes}")
    print(f"  Total virtual nodes: {ring.total_virtual_nodes}")

    assignment_after_remove = dict(ring.iter_assignments(keys))

    redist_remove = consistent_hashing.stats.compute_redistribution(
        keys, assignment_before_remove, assignment_after_remove, strict=True
    )
    moved_count_remove = redist_remove["total_moved"]
    print(
        f"\n  Keys redistributed: {moved_count_remove}/{len(keys)}"
        f" ({moved_count_remove / len(keys) * 100:.1f}%)"
    )

    # Show where removed server's keys went
    moved_remove = redist_remove["moved"]
    destinations: dict[str, int] = {}
    for info in moved_remove.values():
        dest = info["to"]
        destinations[dest] = destinations.get(dest, 0) + 1

    print("\n  Keys from 'web-2' redistributed to:")
    for dest, count in sorted(destinations.items()):
        print(f"    {dest}: {count} keys")

    # --- 7. Distribution After Removing ---
    print_separator("7. Distribution Statistics (3 Servers, after removal)")

    stats_after_remove = compute_distribution(ring, keys)
    print_stats(stats_after_remove)

    # --- 8. Summary ---
    print_separator("8. Summary")
    print("  This sample demonstrates:")
    print("    - Deterministic key-to-server mapping")
    print("    - Minimal redistribution on topology changes")
    print("    - Measured key balance with virtual nodes")
    print(
        f"\n  Adding 'web-4': only {moved_count / len(keys) * 100:.1f}% of keys"
        " moved (ideal: 25%)"
    )
    print(
        f"  Removing 'web-2': only {moved_count_remove / len(keys) * 100:.1f}% of"
        " keys moved"
    )
    print()


if __name__ == "__main__":
    main()
