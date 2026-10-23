"""Regression tests for topology publication and hash collisions."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from consistent_hashing.ring import ConsistentHashRing


def test_colliding_nodes_remain_independently_removable():
    ring = ConsistentHashRing(["b", "a"], 3, lambda key: 7)
    assert ring.get_nodes("key", 10) == ["a", "b"]
    assert ring.remove_node("a") == [7, 7, 7]
    assert ring.get_node("key") == "b"
    assert ring.total_virtual_nodes == 3


def test_failed_hash_does_not_publish_partial_topology():
    def hash_key(key):
        if key == "bad#1":
            raise RuntimeError("injected hash failure")
        return len(key)

    ring = ConsistentHashRing(["stable"], 2, hash_key)
    with pytest.raises(RuntimeError, match="injected"):
        ring.add_node("bad")
    assert ring.nodes == ["stable"]
    assert ring.total_virtual_nodes == 2
    assert ring.get_node("key") == "stable"


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
def test_invalid_virtual_count(count):
    with pytest.raises(ValueError):
        ConsistentHashRing(num_virtual_nodes=count)


def test_zero_replicas_and_invalid_count():
    ring = ConsistentHashRing(["a"])
    assert ring.get_nodes("key", 0) == []
    with pytest.raises(ValueError):
        ring.get_nodes("key", -1)


def test_slow_writer_does_not_block_readers():
    entered, release = threading.Event(), threading.Event()

    def hash_key(key):
        if key == "new#0":
            entered.set()
            assert release.wait(5)
        return len(key)

    ring = ConsistentHashRing(["stable"], 2, hash_key)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(ring.add_node, "new")
        try:
            assert entered.wait(2)
            assert pool.submit(ring.get_node, "key").result(2) == "stable"
        finally:
            release.set()
        writer.result(2)


def test_concurrent_topology_updates_and_replica_reads():
    ring = ConsistentHashRing(["stable"], 8)
    barrier = threading.Barrier(5)

    def write():
        barrier.wait()
        for index in range(150):
            ring.add_node("temporary", 8)
            ring.remove_node("temporary")

    def read():
        barrier.wait()
        for index in range(1500):
            owners = ring.get_nodes(str(index), 2)
            assert "stable" in owners
            assert len(owners) == len(set(owners))

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(write)] + [pool.submit(read) for _ in range(4)]
        for future in futures:
            future.result(15)
    assert ring.nodes == ["stable"]
