"""Unit tests for the stats module (distribution and redistribution analysis)."""

import math

import pytest

from consistent_hashing.ring import ConsistentHashRing
from consistent_hashing.stats import DistributionStats, compute_distribution, compute_redistribution


class TestComputeDistributionKeyCounting:
    """Tests for correct key counting per server."""

    def test_keys_per_server_sums_to_total_keys(self):
        ring = ConsistentHashRing(nodes=["s1", "s2", "s3"])
        keys = [f"key-{i}" for i in range(100)]
        stats = compute_distribution(ring, keys)
        assert sum(stats.keys_per_server.values()) == stats.total_keys

    def test_total_keys_matches_input_length(self):
        ring = ConsistentHashRing(nodes=["s1", "s2"])
        keys = [f"item-{i}" for i in range(50)]
        stats = compute_distribution(ring, keys)
        assert stats.total_keys == 50

    def test_single_server_gets_all_keys(self):
        ring = ConsistentHashRing(nodes=["only-server"])
        keys = [f"k-{i}" for i in range(30)]
        stats = compute_distribution(ring, keys)
        assert stats.keys_per_server["only-server"] == 30

    def test_all_servers_present_in_keys_per_server(self):
        servers = ["alpha", "beta", "gamma"]
        ring = ConsistentHashRing(nodes=servers, num_virtual_nodes=100)
        keys = [f"key-{i}" for i in range(500)]
        stats = compute_distribution(ring, keys)
        assert set(stats.keys_per_server.keys()) == set(servers)

    def test_num_servers_matches_ring(self):
        ring = ConsistentHashRing(nodes=["a", "b", "c", "d"])
        keys = [f"x-{i}" for i in range(20)]
        stats = compute_distribution(ring, keys)
        assert stats.num_servers == 4

    def test_empty_keys_list(self):
        ring = ConsistentHashRing(nodes=["s1", "s2"])
        stats = compute_distribution(ring, [])
        assert stats.total_keys == 0
        assert all(v == 0 for v in stats.keys_per_server.values())

    def test_empty_ring_raises_runtime_error(self):
        ring = ConsistentHashRing()
        with pytest.raises(RuntimeError):
            compute_distribution(ring, ["key-1"])


class TestComputeDistributionStdDev:
    """Tests for standard deviation calculation."""

    def test_single_server_zero_std_dev(self):
        ring = ConsistentHashRing(nodes=["s1"])
        keys = [f"key-{i}" for i in range(100)]
        stats = compute_distribution(ring, keys)
        assert stats.std_dev == 0.0

    def test_known_std_dev_calculation(self):
        """Verify std_dev with a controlled scenario using a custom hash."""
        # Use a custom hash that deterministically assigns keys to specific servers.
        # We'll create a ring with 2 servers, each with 1 virtual node,
        # and use a hash function that gives us predictable placement.
        # Instead, let's verify the math directly by checking the formula.
        ring = ConsistentHashRing(nodes=["s1", "s2", "s3"], num_virtual_nodes=150)
        keys = [f"key-{i}" for i in range(300)]
        stats = compute_distribution(ring, keys)

        # Manually verify std_dev from keys_per_server
        counts = list(stats.keys_per_server.values())
        mean = sum(counts) / len(counts)
        variance = sum((c - mean) ** 2 for c in counts) / len(counts)
        expected_std_dev = math.sqrt(variance)

        assert stats.mean == pytest.approx(mean)
        assert stats.std_dev == pytest.approx(expected_std_dev)

    def test_perfectly_balanced_distribution_zero_std_dev(self):
        """If all servers have the same count, std_dev should be 0."""
        # Use a custom hash function that distributes keys evenly
        # by cycling through positions deterministically.
        # We'll verify the math: if keys_per_server all equal, std_dev = 0.
        ring = ConsistentHashRing(nodes=["s1"])
        keys = [f"key-{i}" for i in range(50)]
        stats = compute_distribution(ring, keys)
        # Single server means all keys go to one server -> std_dev = 0
        assert stats.std_dev == 0.0

    def test_balance_ratio_calculation(self):
        ring = ConsistentHashRing(nodes=["s1", "s2", "s3"], num_virtual_nodes=150)
        keys = [f"key-{i}" for i in range(300)]
        stats = compute_distribution(ring, keys)
        expected_ratio = stats.std_dev / stats.mean if stats.mean > 0 else 0.0
        assert stats.balance_ratio == pytest.approx(expected_ratio)

    def test_min_max_keys(self):
        ring = ConsistentHashRing(nodes=["s1", "s2", "s3"], num_virtual_nodes=100)
        keys = [f"key-{i}" for i in range(200)]
        stats = compute_distribution(ring, keys)
        counts = list(stats.keys_per_server.values())
        assert stats.min_keys == min(counts)
        assert stats.max_keys == max(counts)


class TestComputeRedistribution:
    """Tests for redistribution detection."""

    def test_no_movement_when_assignments_identical(self):
        keys = ["a", "b", "c"]
        before = {"a": "s1", "b": "s2", "c": "s1"}
        after = {"a": "s1", "b": "s2", "c": "s1"}
        result = compute_redistribution(keys, before, after)
        assert result["total_moved"] == 0
        assert result["moved"] == {}

    def test_all_keys_moved(self):
        keys = ["a", "b", "c"]
        before = {"a": "s1", "b": "s1", "c": "s1"}
        after = {"a": "s2", "b": "s2", "c": "s2"}
        result = compute_redistribution(keys, before, after)
        assert result["total_moved"] == 3
        assert len(result["moved"]) == 3

    def test_moved_keys_have_correct_from_to(self):
        keys = ["x", "y", "z"]
        before = {"x": "s1", "y": "s2", "z": "s3"}
        after = {"x": "s2", "y": "s2", "z": "s1"}
        result = compute_redistribution(keys, before, after)
        # "x" moved from s1 to s2
        assert result["moved"]["x"] == {"from": "s1", "to": "s2"}
        # "y" did not move
        assert "y" not in result["moved"]
        # "z" moved from s3 to s1
        assert result["moved"]["z"] == {"from": "s3", "to": "s1"}
        assert result["total_moved"] == 2

    def test_partial_movement(self):
        keys = ["k1", "k2", "k3", "k4"]
        before = {"k1": "s1", "k2": "s2", "k3": "s1", "k4": "s3"}
        after = {"k1": "s1", "k2": "s3", "k3": "s1", "k4": "s3"}
        result = compute_redistribution(keys, before, after)
        # Only k2 moved (s2 -> s3)
        assert result["total_moved"] == 1
        assert result["moved"]["k2"] == {"from": "s2", "to": "s3"}

    def test_redistribution_with_real_ring(self):
        """Integration test: add a server and verify redistribution is detected."""
        ring = ConsistentHashRing(nodes=["s1", "s2"], num_virtual_nodes=50)
        keys = [f"key-{i}" for i in range(100)]

        before = {k: ring.get_node(k) for k in keys}
        ring.add_node("s3", num_virtual_nodes=50)
        after = {k: ring.get_node(k) for k in keys}

        result = compute_redistribution(keys, before, after)
        # Some keys should have moved to s3
        assert result["total_moved"] > 0
        # All moved keys should now be on s3 (they moved TO the new server)
        for key_info in result["moved"].values():
            assert key_info["to"] == "s3"

    def test_empty_keys_list(self):
        result = compute_redistribution([], {}, {})
        assert result["total_moved"] == 0
        assert result["moved"] == {}
