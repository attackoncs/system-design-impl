"""Chapter 06 affected-range semantics and stable batch statistics."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import threading

import pytest

from consistent_hashing import (
    ConsistentHashRing,
    affected_ranges,
    compute_distribution,
    sha1_hash,
)


def small_hash(key):
    # Force collisions and exercise every integer position in a tiny hash space.
    return int(key[1:]) if key.startswith("@") else sha1_hash(key) % 17


@pytest.mark.parametrize(
    "initial,add,remove",
    [
        ([], ["a"], []),
        (["a"], [], ["a"]),
        (["a", "b"], ["c"], []),
        (["a", "b", "c"], [], ["b"]),
        (["a", "b"], ["c", "d"], ["a"]),
        ([], [], []),
    ],
)
@pytest.mark.parametrize("virtual_nodes", [1, 3, 20])
def test_ranges_exactly_match_changed_owners(initial, add, remove, virtual_nodes):
    ring = ConsistentHashRing(initial, virtual_nodes, small_hash)
    before = ring.snapshot()
    for node in add:
        ring.add_node(node)
    for node in remove:
        ring.remove_node(node)
    after = ring.snapshot()
    ranges = affected_ranges(before, after)
    for position in range(-1, 19):
        old = before.get_node(f"@{position}") if before.nodes else None
        new = after.get_node(f"@{position}") if after.nodes else None
        matches = [item for item in ranges if item.contains(position)]
        assert len(matches) == int(old != new)
        if matches:
            assert (matches[0].previous_owner, matches[0].new_owner) == (old, new)
    if add and not remove and initial:
        assert all(item.new_owner in add for item in ranges)
    if remove and not add:
        assert all(item.previous_owner in remove for item in ranges)


def test_snapshot_remains_usable_after_live_ring_becomes_empty():
    ring = ConsistentHashRing(["a", "b"])
    view = ring.snapshot()
    expected = [view.get_node(str(i)) for i in range(100)]
    ring.remove_node("a")
    ring.remove_node("b")
    assert [view.get_node(str(i)) for i in range(100)] == expected
    assert compute_distribution(view, ["x"]).num_servers == 2
    with pytest.raises(FrozenInstanceError):
        view._state = None


def test_distribution_pins_one_topology_during_concurrent_update():
    entered, release = threading.Event(), threading.Event()

    def blocking_hash(key):
        if key == "pause":
            entered.set()
            assert release.wait(3)
        return sha1_hash(key)

    ring = ConsistentHashRing(["a", "b"], hash_function=blocking_hash)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(compute_distribution, ring, ["pause", "other"])
        try:
            assert entered.wait(1)
            ring.remove_node("a")
            ring.remove_node("b")
        finally:
            release.set()
        stats = result.result(2)
    assert set(stats.keys_per_server) == {"a", "b"}
    assert sum(stats.keys_per_server.values()) == 2


def test_migration_rejects_incompatible_hash_spaces():
    first = ConsistentHashRing(["a"]).snapshot()
    second = ConsistentHashRing(["a"], hash_function=small_hash).snapshot()
    with pytest.raises(ValueError, match="same hash function"):
        affected_ranges(first, second)


def test_full_collision_interval_and_exact_boundary():
    def constant_hash(key):
        return 7

    ring = ConsistentHashRing(["b"], hash_function=constant_hash)
    before = ring.snapshot()
    ring.add_node("a")
    ranges = affected_ranges(before, ring.snapshot())
    assert len(ranges) == 1
    assert ranges[0].start == ranges[0].end == 7
    assert all(ranges[0].contains(value) for value in [-1, 0, 7, 8, 100])
    assert ranges[0].new_owner == "a"
