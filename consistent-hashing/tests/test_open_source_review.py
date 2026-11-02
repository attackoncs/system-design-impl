"""Independent routing oracle and limits highlighted by open-source comparison."""

import pytest

from consistent_hashing import (
    ConsistentHashRing,
    affected_ranges,
    compute_distribution,
    iter_migrations,
    md5_hash,
    sha1_hash,
    sha256_hash,
)


def colliding_hash(key):
    return sha1_hash(key) % 7 - 3


@pytest.mark.parametrize(
    "hash_function", [sha1_hash, md5_hash, sha256_hash, colliding_hash]
)
def test_routes_and_migrations_match_linear_oracle(hash_function):
    weights = {"cache": 3, "缓存#0": 3}
    ring = ConsistentHashRing(list(weights), 3, hash_function)
    keys = ["", "用户:42", "cache#0", "缓存#0#1"] + [f"key-{i}" for i in range(25)]
    previous = None
    previous_routes = {}

    # Exercise joins, growth, shrinkage and departure, including token collisions.
    for node, count in [
        (None, 0),
        ("cache#0", 4),
        ("cache", 7),
        ("cache", 1),
        ("缓存#0", 0),
    ]:
        if node is not None:
            if count == 0:
                ring.remove_node(node)
                del weights[node]
            elif node in weights:
                ring.set_virtual_nodes(node, count)
                weights[node] = count
            else:
                ring.add_node(node, count)
                weights[node] = count
        view = ring.snapshot()
        entries = sorted(
            (hash_function(f"{name}#{i}"), name, i)
            for name, size in weights.items()
            for i in range(size)
        )
        routes = {}
        for key in keys:
            position = hash_function(key)
            # Linear partitioning is independent of the implementation's bisect.
            clockwise = [item for item in entries if item[0] > position]
            clockwise += [item for item in entries if item[0] <= position]
            candidates = list(dict.fromkeys(item[1] for item in clockwise))
            routes[key] = candidates[0]
            assert view.get_node(key) == candidates[0]
            for replica_count in (0, 1, len(weights) + 2):
                assert view.get_nodes(key, replica_count) == candidates[:replica_count]
        if previous is not None:
            expected = [
                (key, previous_routes[key], routes[key])
                for key in keys
                if previous_routes[key] != routes[key]
            ]
            assert [
                (item.key, item.previous_owner, item.new_owner)
                for item in iter_migrations(previous, view, iter(keys))
            ] == expected
            assert dict(previous.iter_assignments(keys)) == previous_routes
        previous, previous_routes = view, routes


def test_all_candidates_can_move_on_addition():
    positions = {"a#0": 10, "b#0": 50, "d#0": 70, "e#0": 90, "c#0": 30}

    def hash_key(key):
        return positions.get(key, 20)

    ring = ConsistentHashRing(["a", "b", "d", "e"], 1, hash_key)
    before = ring.snapshot()
    ring.add_node("c")
    keys = [f"key-{i}" for i in range(100)]
    moved = list(iter_migrations(before, ring.snapshot(), keys))
    assert len(moved) == len(keys)
    assert {(item.previous_owner, item.new_owner) for item in moved} == {("b", "c")}


def test_a_colliding_member_can_have_no_primary_keys():
    ring = ConsistentHashRing(["a", "b"], 3, lambda key: 7)
    stats = compute_distribution(ring, (f"key-{i}" for i in range(100)))
    assert stats.keys_per_server == {"a": 100, "b": 0}
    assert ring.get_nodes("key", 2) == ["a", "b"]
    ring.remove_node("a")
    assert ring.get_node("key") == "b"


@pytest.mark.parametrize("nodes", [[], ["a"], ["a", "b"]])
def test_unchanged_plans_do_not_consume_candidates(nodes):
    ring = ConsistentHashRing(nodes, 3)
    before = ring.snapshot()
    if nodes:
        ring.set_virtual_nodes(nodes[0], 3)

    def candidates():
        raise AssertionError("an unchanged plan must not consume candidates")
        yield "unused"

    for after in (ring.snapshot(), ConsistentHashRing(nodes, 3).snapshot()):
        assert affected_ranges(before, after) == []
        assert list(iter_migrations(before, after, candidates())) == []
