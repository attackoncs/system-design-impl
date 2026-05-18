"""Tests for cluster membership management."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kv_store.cluster.gossip import GossipProtocol, MemberInfo, NodeStatus
from kv_store.cluster.membership import ClusterMembership


class FakeHashRing:
    """A simple fake hash ring for testing."""

    def __init__(self):
        self._nodes: list[str] = []

    def add_node(self, node: str, num_virtual_nodes=None) -> list[int]:
        if node not in self._nodes:
            self._nodes.append(node)
        return [0]

    def remove_node(self, node: str) -> list[int]:
        if node in self._nodes:
            self._nodes.remove(node)
            return [0]
        raise KeyError(f"Node '{node}' does not exist on the ring.")

    @property
    def nodes(self) -> list[str]:
        return list(self._nodes)


@pytest.fixture
def gossip():
    """Create a GossipProtocol instance."""
    return GossipProtocol(
        node_id="node-1",
        address="localhost:5001",
        gossip_interval=1.0,
        gossip_fanout=2,
        failure_timeout=5.0,
        gossip_func=AsyncMock(),
    )


@pytest.fixture
def hash_ring():
    """Create a fake hash ring."""
    return FakeHashRing()


@pytest.fixture
def membership(gossip, hash_ring):
    """Create a ClusterMembership instance."""
    return ClusterMembership(
        node_id="node-1",
        address="localhost:5001",
        gossip=gossip,
        hash_ring=hash_ring,
        virtual_nodes=10,
    )


class TestJoinCluster:
    """Tests for joining the cluster."""

    async def test_join_adds_node_to_hash_ring(self, membership, hash_ring):
        """Joining should add this node to the hash ring."""
        await membership.join_cluster()
        assert "node-1" in hash_ring.nodes

    async def test_join_starts_gossip(self, membership, gossip):
        """Joining should start the gossip protocol."""
        with patch.object(gossip, "start", new_callable=AsyncMock) as mock_start:
            await membership.join_cluster()
            mock_start.assert_called_once()

    async def test_join_with_seed_nodes(self, membership, gossip):
        """Joining with seed nodes should add them to gossip."""
        seeds = [("node-2", "localhost:5002"), ("node-3", "localhost:5003")]
        await membership.join_cluster(seed_nodes=seeds)
        assert "node-2" in gossip.members
        assert "node-3" in gossip.members

    async def test_join_is_idempotent(self, membership, hash_ring):
        """Joining twice should not cause errors."""
        await membership.join_cluster()
        await membership.join_cluster()
        assert hash_ring.nodes.count("node-1") == 1


class TestLeaveCluster:
    """Tests for leaving the cluster."""

    async def test_leave_removes_node_from_hash_ring(self, membership, hash_ring):
        """Leaving should remove this node from the hash ring."""
        await membership.join_cluster()
        assert "node-1" in hash_ring.nodes
        await membership.leave_cluster()
        assert "node-1" not in hash_ring.nodes

    async def test_leave_stops_gossip(self, membership, gossip):
        """Leaving should stop the gossip protocol."""
        await membership.join_cluster()
        with patch.object(gossip, "stop", new_callable=AsyncMock) as mock_stop:
            await membership.leave_cluster()
            mock_stop.assert_called_once()

    async def test_leave_without_join(self, membership, hash_ring):
        """Leaving without joining should be a no-op."""
        await membership.leave_cluster()
        assert "node-1" not in hash_ring.nodes


class TestNodeFailure:
    """Tests for node failure handling."""

    async def test_node_failure_removes_from_ring(self, membership, hash_ring):
        """on_node_failed should remove the node from the hash ring."""
        await membership.join_cluster()
        membership.on_node_joined("node-2", "localhost:5002")
        assert "node-2" in hash_ring.nodes

        membership.on_node_failed("node-2")
        assert "node-2" not in hash_ring.nodes

    async def test_node_failure_unknown_node(self, membership, hash_ring):
        """on_node_failed for unknown node should not raise."""
        await membership.join_cluster()
        # Should not raise
        membership.on_node_failed("unknown-node")


class TestGetAliveMembers:
    """Tests for get_alive_members."""

    async def test_get_alive_members(self, membership, gossip):
        """Should return only alive members."""
        await membership.join_cluster()
        gossip.add_seed_node("node-2", "localhost:5002")
        gossip.add_seed_node("node-3", "localhost:5003")
        gossip._members["node-3"].status = NodeStatus.SUSPECTED

        alive = membership.get_alive_members()
        alive_ids = [m.node_id for m in alive]
        assert "node-1" in alive_ids
        assert "node-2" in alive_ids
        assert "node-3" not in alive_ids


class TestGetNodeAddress:
    """Tests for get_node_address."""

    async def test_get_node_address_known(self, membership, gossip):
        """Should return address for known nodes."""
        gossip.add_seed_node("node-2", "localhost:5002")
        assert membership.get_node_address("node-2") == "localhost:5002"

    async def test_get_node_address_unknown(self, membership):
        """Should return None for unknown nodes."""
        assert membership.get_node_address("unknown") is None

    async def test_get_node_address_self(self, membership):
        """Should return own address."""
        assert membership.get_node_address("node-1") == "localhost:5001"


class TestOnNodeJoined:
    """Tests for on_node_joined."""

    async def test_on_node_joined_adds_to_ring(self, membership, hash_ring):
        """on_node_joined should add the node to the hash ring."""
        await membership.join_cluster()
        membership.on_node_joined("node-2", "localhost:5002")
        assert "node-2" in hash_ring.nodes

    async def test_on_node_joined_adds_to_gossip(self, membership, gossip):
        """on_node_joined should add the node to gossip membership."""
        membership.on_node_joined("node-2", "localhost:5002")
        assert "node-2" in gossip.members


class TestIsNodeAlive:
    """Tests for is_node_alive."""

    async def test_is_node_alive_true(self, membership, gossip):
        """Should return True for alive nodes."""
        gossip.add_seed_node("node-2", "localhost:5002")
        assert membership.is_node_alive("node-2") is True

    async def test_is_node_alive_false_suspected(self, membership, gossip):
        """Should return False for suspected nodes."""
        gossip.add_seed_node("node-2", "localhost:5002")
        gossip._members["node-2"].status = NodeStatus.SUSPECTED
        assert membership.is_node_alive("node-2") is False

    async def test_is_node_alive_unknown(self, membership):
        """Should return False for unknown nodes."""
        assert membership.is_node_alive("unknown") is False
