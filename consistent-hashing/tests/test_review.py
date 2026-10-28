"""Review regressions for resource limits, streaming views and weight changes."""

from concurrent.futures import ThreadPoolExecutor
import random
import threading

import pytest

from consistent_hashing import (
    ConsistentHashRing,
    affected_ranges,
    iter_migrations,
    compute_distribution,
    compute_redistribution,
    sha1_hash,
)


@pytest.mark.parametrize(
    "nodes", ["abc", b"abc", False, {"a", "b"}, ["a", "a"], ["a", 1]]
)
def test_constructor_rejects_ambiguous_or_invalid_membership(nodes):
    with pytest.raises(ValueError):
        ConsistentHashRing(nodes)


@pytest.mark.parametrize("function", [0, "sha1", False, object()])
def test_constructor_validates_hash_callable_even_for_an_empty_ring(function):
    with pytest.raises(ValueError, match="callable"):
        ConsistentHashRing(hash_function=function)


def test_falsey_custom_callable_is_not_replaced_by_the_default():
    class Hash:
        def __bool__(self):
            return False

        def __call__(self, key):
            return 7

    ring = ConsistentHashRing(["b", "a"], 1, Hash())
    assert ring.get_node("key") == "a"
    assert ring.remove_node("a") == [7]


def test_bulk_construction_matches_incremental_construction_with_collisions():
    def hash_key(key):
        return sha1_hash(key) % 13

    bulk = ConsistentHashRing(("c", "b", "a"), 7, hash_key)
    incremental = ConsistentHashRing(num_virtual_nodes=7, hash_function=hash_key)
    for node in ("c", "b", "a"):
        incremental.add_node(node)
    assert bulk.nodes == incremental.nodes
    for index in range(100):
        assert bulk.get_nodes(str(index), 3) == incremental.get_nodes(str(index), 3)


def test_capacity_fails_before_hashing_or_publishing():
    calls = []

    def hash_key(key):
        calls.append(key)
        return sha1_hash(key)

    with pytest.raises(ValueError, match="capacity"):
        ConsistentHashRing(["a", "b"], 2, hash_key, max_virtual_nodes=3)
    assert calls == []
    ring = ConsistentHashRing(["a"], 2, hash_key, max_virtual_nodes=3)
    view = ring.snapshot()
    calls.clear()
    for action in (
        lambda: ring.add_node("b", 2),
        lambda: ring.set_virtual_nodes("a", 4),
    ):
        with pytest.raises(ValueError, match="capacity"):
            action()
    assert calls == []
    assert ring.snapshot()._state is view._state


def test_concurrent_writers_cannot_exceed_capacity():
    ring = ConsistentHashRing(["stable"], 1, max_virtual_nodes=3)
    barrier = threading.Barrier(2)

    def add(node):
        barrier.wait(timeout=2)
        try:
            ring.add_node(node, 2)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(add, ("a", "b")))
    assert results.count(True) == 1
    assert ring.total_virtual_nodes == 3
    assert "stable" in ring.nodes


def test_stream_captures_topology_at_call_time_and_consumes_once():
    ring = ConsistentHashRing(["a", "b"], 5)
    view = ring.snapshot()
    consumed = []

    def keys():
        for key in ("x", "y", "x"):
            consumed.append(key)
            yield key

    stream = ring.iter_assignments(keys())
    assert not consumed
    ring.remove_node("a")
    ring.remove_node("b")
    assert list(stream) == [(key, view.get_node(key)) for key in ("x", "y", "x")]
    assert consumed == ["x", "y", "x"]
    assert list(stream) == []


def test_get_keys_filters_supplied_keys_without_a_data_registry():
    ring = ConsistentHashRing(["a", "b"])
    keys = [str(index) for index in range(100)]
    expected = [key for key in keys if ring.get_node(key) == "a"]
    assert ring.get_keys("a", iter(keys)) == expected
    assert ring.snapshot().get_keys("a", []) == []
    with pytest.raises(KeyError):
        ring.get_keys("unknown", keys)
    with pytest.raises(RuntimeError):
        ConsistentHashRing().iter_assignments([])


def test_weight_change_only_hashes_new_indices_and_preserves_old_views():
    calls = []
    failing = [False]

    def hash_key(key):
        calls.append(key)
        if failing[0]:
            raise AssertionError("existing positions must not be rehashed")
        return sha1_hash(key)

    ring = ConsistentHashRing(["a", "b"], 2, hash_key)
    view = ring.snapshot()
    calls.clear()
    ring.set_virtual_nodes("a", 5)
    assert calls == ["a#2", "a#3", "a#4"]
    assert ring.total_virtual_nodes == 7
    assert view.total_virtual_nodes == 4
    failing[0] = True
    ring.set_virtual_nodes("a", 5)
    ring.set_virtual_nodes("a", 2)
    assert ring.snapshot()._state.entries == view._state.entries
    assert ring.total_virtual_nodes == 4


def test_weight_hash_failure_does_not_publish_partial_state():
    def hash_key(key):
        if key == "a#3":
            raise RuntimeError("injected")
        return sha1_hash(key)

    ring = ConsistentHashRing(["a", "b"], 2, hash_key)
    view = ring.snapshot()
    with pytest.raises(RuntimeError, match="injected"):
        ring.set_virtual_nodes("a", 5)
    assert ring.snapshot()._state is view._state
    with pytest.raises(KeyError):
        ring.set_virtual_nodes("missing", 1)


def test_slow_weight_writer_leaves_complete_old_topology_readable():
    entered, release = threading.Event(), threading.Event()

    def hash_key(key):
        if key == "a#2":
            entered.set()
            assert release.wait(3)
        return sha1_hash(key)

    ring = ConsistentHashRing(["a", "b"], 2, hash_key)
    with ThreadPoolExecutor(2) as pool:
        writer = pool.submit(ring.set_virtual_nodes, "a", 4)
        try:
            assert entered.wait(1)
            current = pool.submit(ring.snapshot).result(timeout=1)
            assert current.total_virtual_nodes == 4 and current.nodes == ["a", "b"]
        finally:
            release.set()
        writer.result(timeout=2)
    assert ring.total_virtual_nodes == 6
    assert current.total_virtual_nodes == 4


@pytest.mark.parametrize("seed", range(8))
def test_range_plans_match_actual_routes_during_weight_changes(seed):
    source = random.Random(seed)

    def hash_key(key):
        return int(key[1:]) if key.startswith("@") else sha1_hash(key) % 11 - 5

    ring = ConsistentHashRing(["a", "b", "c"], 3, hash_key)
    for _ in range(12):
        before = ring.snapshot()
        ring.set_virtual_nodes(source.choice(ring.nodes), source.randint(1, 9))
        after = ring.snapshot()
        plan = affected_ranges(before, after)
        for position in range(-7, 8):
            old, new = before.get_node(f"@{position}"), after.get_node(f"@{position}")
            matches = [item for item in plan if item.contains(position)]
            assert len(matches) == (old != new)
            if matches:
                assert (matches[0].previous_owner, matches[0].new_owner) == (old, new)


def test_distribution_accepts_one_pass_inputs_and_pins_before_consumption():
    ring = ConsistentHashRing(["a", "b"])

    def keys():
        ring.remove_node("a")
        ring.remove_node("b")
        yield "one"
        yield "one"
        yield "two"

    stats = compute_distribution(ring, keys())
    assert stats.total_keys == 3
    assert stats.num_servers == 2
    assert sum(stats.keys_per_server.values()) == 3
    assert ring.nodes == []


def test_strict_redistribution_detects_missing_and_unassigned_transitions():
    with pytest.raises(KeyError, match="missing assignment"):
        compute_redistribution(["lost"], {"lost": "a"}, {}, strict=True)
    result = compute_redistribution(
        iter(["bootstrap", "drain"]),
        {"bootstrap": None, "drain": "a"},
        {"bootstrap": "b", "drain": None},
        strict=True,
    )
    assert result == {
        "moved": {
            "bootstrap": {"from": None, "to": "b"},
            "drain": {"from": "a", "to": None},
        },
        "total_moved": 2,
    }


@pytest.mark.parametrize("value", [1, b"key", None, False])
def test_key_contract_is_consistent_with_custom_hashes(value):
    ring = ConsistentHashRing(["a"], 1, lambda key: 1)
    with pytest.raises(ValueError, match="key must be a string"):
        ring.get_node(value)


def test_hash_callback_writer_reentry_fails_without_deadlock_or_partial_state():
    holder = {}

    def hash_key(key):
        if key == "bad#0":
            holder["ring"].remove_node("stable")
        return sha1_hash(key)

    ring = ConsistentHashRing(["stable"], 1, hash_key)
    holder["ring"] = ring
    view = ring.snapshot()
    with pytest.raises(RuntimeError, match="reenter"):
        ring.add_node("bad")
    assert ring.snapshot()._state is view._state
    ring.add_node("next")
    assert ring.nodes == ["stable", "next"]


def test_migration_requires_explicit_snapshots_and_strict_maps_validate_owners():
    ring = ConsistentHashRing(["a"])
    with pytest.raises(TypeError, match="RingSnapshot"):
        affected_ranges(ring, ring.snapshot())
    with pytest.raises(ValueError, match="owners"):
        compute_redistribution(["x"], {"x": 42}, {"x": "a"}, strict=True)


@pytest.mark.parametrize(
    "initial,final",
    [([], ["a"]), (["a"], []), (["a"], ["a", "b"]), (["a", "b"], ["b"]), ([], [])],
)
@pytest.mark.parametrize("collisions", [False, True])
def test_streamed_migrations_match_routing_including_empty_topologies(
    initial, final, collisions
):
    def hash_key(key):
        if key.startswith("@"):
            return int(key[1:])
        return 0 if collisions else sha1_hash(key) % 11

    before = ConsistentHashRing(initial, 3, hash_key).snapshot()
    after = ConsistentHashRing(final, 3, hash_key).snapshot()
    keys = [f"@{index}" for index in range(-2, 13)] * 2
    expected = [
        (
            key,
            before.get_node(key) if initial else None,
            after.get_node(key) if final else None,
        )
        for key in keys
    ]
    expected = [item for item in expected if item[1] != item[2]]
    actual = [
        (item.key, item.previous_owner, item.new_owner)
        for item in iter_migrations(before, after, iter(keys))
    ]
    assert actual == expected


def test_migration_selection_is_lazy_pinned_and_hashes_each_candidate_once():
    calls = []

    def hash_key(key):
        calls.append(key)
        return sha1_hash(key)

    ring = ConsistentHashRing(["a"], 2, hash_key)
    before = ring.snapshot()
    ring.add_node("b")
    after = ring.snapshot()
    calls.clear()
    keys = ["x", "y", "z"]
    stream = iter_migrations(before, after, iter(keys))
    assert calls == []
    ring.remove_node("a")
    ring.remove_node("b")
    list(stream)
    assert calls == keys
    calls.clear()
    assert list(iter_migrations(before, before, iter(keys))) == []
    assert calls == []
