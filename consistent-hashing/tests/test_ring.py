"""Unit tests for the ConsistentHashRing class."""

import pytest

from consistent_hashing.ring import ConsistentHashRing


class TestAddNode:
    """Tests for adding nodes to the ring."""

    def test_add_single_node(self):
        ring = ConsistentHashRing()
        ring.add_node("server-1")
        assert "server-1" in ring.nodes
        assert ring.total_virtual_nodes == 150

    def test_add_multiple_nodes(self):
        ring = ConsistentHashRing()
        ring.add_node("server-1")
        ring.add_node("server-2")
        ring.add_node("server-3")
        assert len(ring.nodes) == 3
        assert ring.total_virtual_nodes == 450

    def test_add_node_with_custom_virtual_nodes(self):
        ring = ConsistentHashRing()
        ring.add_node("server-1", num_virtual_nodes=50)
        assert ring.total_virtual_nodes == 50

    def test_add_node_returns_positions(self):
        ring = ConsistentHashRing()
        positions = ring.add_node("server-1", num_virtual_nodes=5)
        assert len(positions) == 5
        assert all(isinstance(p, int) for p in positions)

    def test_add_duplicate_node_raises_value_error(self):
        ring = ConsistentHashRing()
        ring.add_node("server-1")
        with pytest.raises(ValueError, match="already exists"):
            ring.add_node("server-1")

    def test_add_nodes_via_constructor(self):
        ring = ConsistentHashRing(nodes=["a", "b", "c"])
        assert len(ring.nodes) == 3
        assert ring.total_virtual_nodes == 450


class TestRemoveNode:
    """Tests for removing nodes from the ring."""

    def test_remove_existing_node(self):
        ring = ConsistentHashRing(nodes=["server-1", "server-2"])
        ring.remove_node("server-1")
        assert "server-1" not in ring.nodes
        assert len(ring.nodes) == 1
        assert ring.total_virtual_nodes == 150

    def test_remove_node_returns_positions(self):
        ring = ConsistentHashRing()
        ring.add_node("server-1", num_virtual_nodes=5)
        positions = ring.remove_node("server-1")
        assert len(positions) == 5

    def test_remove_nonexistent_node_raises_key_error(self):
        ring = ConsistentHashRing(nodes=["server-1"])
        with pytest.raises(KeyError, match="does not exist"):
            ring.remove_node("server-99")

    def test_remove_all_nodes_empties_ring(self):
        ring = ConsistentHashRing(nodes=["server-1"])
        ring.remove_node("server-1")
        assert ring.total_virtual_nodes == 0
        assert ring.nodes == []


class TestGetNode:
    """Tests for single-node key lookup."""

    def test_get_node_determinism(self):
        ring = ConsistentHashRing(nodes=["s1", "s2", "s3"])
        key = "my-key"
        result1 = ring.get_node(key)
        result2 = ring.get_node(key)
        result3 = ring.get_node(key)
        assert result1 == result2 == result3

    def test_get_node_returns_valid_server(self):
        ring = ConsistentHashRing(nodes=["s1", "s2", "s3"])
        for i in range(100):
            node = ring.get_node(f"key-{i}")
            assert node in ["s1", "s2", "s3"]

    def test_get_node_empty_ring_raises_runtime_error(self):
        ring = ConsistentHashRing()
        with pytest.raises(RuntimeError, match="empty"):
            ring.get_node("any-key")

    def test_get_node_single_server_always_returns_it(self):
        ring = ConsistentHashRing(nodes=["only-server"])
        for i in range(50):
            assert ring.get_node(f"key-{i}") == "only-server"

    def test_get_node_stable_after_unrelated_changes(self):
        """Keys not affected by a new node should stay on the same server."""
        ring = ConsistentHashRing(nodes=["s1", "s2"], num_virtual_nodes=50)
        keys = [f"key-{i}" for i in range(200)]
        before = {k: ring.get_node(k) for k in keys}

        ring.add_node("s3", num_virtual_nodes=50)
        after = {k: ring.get_node(k) for k in keys}

        # Some keys may move to s3, but keys that didn't move should stay put
        for k in keys:
            if after[k] != "s3":
                assert after[k] == before[k]


class TestGetNodes:
    """Tests for multi-node key lookup (replication)."""

    def test_get_nodes_returns_distinct_servers(self):
        ring = ConsistentHashRing(nodes=["s1", "s2", "s3"])
        nodes = ring.get_nodes("my-key", 3)
        assert len(nodes) == len(set(nodes))

    def test_get_nodes_count_exceeds_servers(self):
        ring = ConsistentHashRing(nodes=["s1", "s2"])
        nodes = ring.get_nodes("my-key", 5)
        assert len(nodes) == 2

    def test_get_nodes_first_matches_get_node(self):
        ring = ConsistentHashRing(nodes=["s1", "s2", "s3"])
        key = "test-key"
        assert ring.get_nodes(key, 1)[0] == ring.get_node(key)

    def test_get_nodes_empty_ring_raises_runtime_error(self):
        ring = ConsistentHashRing()
        with pytest.raises(RuntimeError, match="empty"):
            ring.get_nodes("key", 2)

    def test_get_nodes_returns_all_servers_when_count_equals_total(self):
        servers = ["s1", "s2", "s3", "s4"]
        ring = ConsistentHashRing(nodes=servers)
        nodes = ring.get_nodes("key", 4)
        assert set(nodes) == set(servers)


class TestWrapAround:
    """Tests for wrap-around behavior on the ring."""

    def test_wrap_around_single_node(self):
        """A single node should handle all keys regardless of position."""
        ring = ConsistentHashRing(nodes=["server-1"], num_virtual_nodes=1)
        # All keys should map to the single server
        for i in range(20):
            assert ring.get_node(f"key-{i}") == "server-1"

    def test_keys_distributed_across_ring(self):
        """Keys should be distributed across multiple servers."""
        ring = ConsistentHashRing(nodes=["s1", "s2", "s3"], num_virtual_nodes=100)
        assignments = set()
        for i in range(100):
            assignments.add(ring.get_node(f"key-{i}"))
        # With 100 keys and 3 servers with 100 vnodes each, all should get some
        assert len(assignments) == 3


class TestProperties:
    """Tests for nodes and total_virtual_nodes properties."""

    def test_nodes_property_empty(self):
        ring = ConsistentHashRing()
        assert ring.nodes == []

    def test_nodes_property_returns_all(self):
        ring = ConsistentHashRing(nodes=["a", "b", "c"])
        assert set(ring.nodes) == {"a", "b", "c"}

    def test_total_virtual_nodes_empty(self):
        ring = ConsistentHashRing()
        assert ring.total_virtual_nodes == 0

    def test_total_virtual_nodes_weighted(self):
        ring = ConsistentHashRing()
        ring.add_node("s1", num_virtual_nodes=100)
        ring.add_node("s2", num_virtual_nodes=200)
        assert ring.total_virtual_nodes == 300
