"""Tests for the gossip protocol implementation."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from kv_store.cluster.gossip import GossipProtocol, MemberInfo, NodeStatus


@pytest.fixture
def gossip():
    """Create a GossipProtocol instance for testing."""
    return GossipProtocol(
        node_id="node-1",
        address="localhost:5001",
        gossip_interval=1.0,
        gossip_fanout=2,
        failure_timeout=5.0,
        gossip_func=AsyncMock(),
    )


class TestHeartbeat:
    """Tests for heartbeat increment behavior."""

    async def test_heartbeat_increments_each_round(self, gossip):
        """Heartbeat counter should increment with each gossip round."""
        initial = gossip.members["node-1"].heartbeat_counter
        await gossip._gossip_round()
        assert gossip.members["node-1"].heartbeat_counter == initial + 1
        await gossip._gossip_round()
        assert gossip.members["node-1"].heartbeat_counter == initial + 2

    async def test_heartbeat_starts_at_zero(self, gossip):
        """Heartbeat counter should start at zero."""
        assert gossip.members["node-1"].heartbeat_counter == 0


class TestMergeMembership:
    """Tests for membership merge logic."""

    async def test_merge_takes_higher_heartbeat(self, gossip):
        """Merge should keep the higher heartbeat counter."""
        # Add a node with heartbeat 5
        gossip.add_seed_node("node-2", "localhost:5002")
        gossip._members["node-2"].heartbeat_counter = 5

        # Merge with remote that has heartbeat 10
        remote = [
            MemberInfo(
                node_id="node-2",
                address="localhost:5002",
                heartbeat_counter=10,
                status=NodeStatus.ALIVE,
            )
        ]
        gossip.merge_membership(remote)
        assert gossip.members["node-2"].heartbeat_counter == 10

    async def test_merge_ignores_lower_heartbeat(self, gossip):
        """Merge should not downgrade heartbeat counter."""
        gossip.add_seed_node("node-2", "localhost:5002")
        gossip._members["node-2"].heartbeat_counter = 10

        remote = [
            MemberInfo(
                node_id="node-2",
                address="localhost:5002",
                heartbeat_counter=5,
                status=NodeStatus.ALIVE,
            )
        ]
        gossip.merge_membership(remote)
        assert gossip.members["node-2"].heartbeat_counter == 10

    async def test_new_nodes_added_via_merge(self, gossip):
        """Merge should add previously unknown nodes."""
        remote = [
            MemberInfo(
                node_id="node-3",
                address="localhost:5003",
                heartbeat_counter=1,
                status=NodeStatus.ALIVE,
            )
        ]
        gossip.merge_membership(remote)
        assert "node-3" in gossip.members
        assert gossip.members["node-3"].address == "localhost:5003"
        assert gossip.members["node-3"].heartbeat_counter == 1

    async def test_merge_does_not_overwrite_self(self, gossip):
        """Merge should not overwrite this node's own info."""
        await gossip._gossip_round()  # increment heartbeat to 1
        remote = [
            MemberInfo(
                node_id="node-1",
                address="localhost:9999",
                heartbeat_counter=100,
                status=NodeStatus.DOWN,
            )
        ]
        gossip.merge_membership(remote)
        # Own info should remain unchanged
        assert gossip.members["node-1"].heartbeat_counter == 1
        assert gossip.members["node-1"].address == "localhost:5001"


class TestFailureDetection:
    """Tests for failure detection via timeouts."""

    async def test_node_marked_suspected_after_timeout(self, gossip):
        """Node should be marked SUSPECTED after failure_timeout."""
        gossip.add_seed_node("node-2", "localhost:5002")
        # Simulate time passing beyond failure timeout
        gossip._members["node-2"].last_updated = time.time() - 6.0
        gossip._check_timeouts()
        assert gossip._members["node-2"].status == NodeStatus.SUSPECTED

    async def test_node_marked_down_after_extended_timeout(self, gossip):
        """Node should be marked DOWN after 2x failure_timeout."""
        gossip.add_seed_node("node-2", "localhost:5002")
        # First mark as suspected
        gossip._members["node-2"].last_updated = time.time() - 6.0
        gossip._check_timeouts()
        assert gossip._members["node-2"].status == NodeStatus.SUSPECTED

        # Now simulate more time passing (total > 2x timeout = 10s)
        gossip._members["node-2"].last_updated = time.time() - 11.0
        gossip._check_timeouts()
        assert gossip._members["node-2"].status == NodeStatus.DOWN

    async def test_alive_node_not_marked_suspected(self, gossip):
        """Node within timeout should remain ALIVE."""
        gossip.add_seed_node("node-2", "localhost:5002")
        gossip._members["node-2"].last_updated = time.time() - 2.0
        gossip._check_timeouts()
        assert gossip._members["node-2"].status == NodeStatus.ALIVE

    async def test_merge_revives_suspected_node(self, gossip):
        """A higher heartbeat from a suspected node should revive it."""
        gossip.add_seed_node("node-2", "localhost:5002")
        gossip._members["node-2"].status = NodeStatus.SUSPECTED
        gossip._members["node-2"].heartbeat_counter = 5

        remote = [
            MemberInfo(
                node_id="node-2",
                address="localhost:5002",
                heartbeat_counter=10,
                status=NodeStatus.ALIVE,
            )
        ]
        gossip.merge_membership(remote)
        assert gossip._members["node-2"].status == NodeStatus.ALIVE


class TestGossipFanout:
    """Tests for peer selection during gossip rounds."""

    async def test_fanout_selects_correct_number_of_peers(self):
        """Gossip should select up to fanout number of peers."""
        gossip = GossipProtocol(
            node_id="node-1",
            address="localhost:5001",
            gossip_fanout=2,
            gossip_func=AsyncMock(),
        )
        # Add 5 peers
        for i in range(2, 7):
            gossip.add_seed_node(f"node-{i}", f"localhost:500{i}")

        peers = gossip._select_peers()
        assert len(peers) == 2  # fanout is 2

    async def test_fanout_limited_by_available_peers(self):
        """Fanout should be limited by the number of available peers."""
        gossip = GossipProtocol(
            node_id="node-1",
            address="localhost:5001",
            gossip_fanout=5,
            gossip_func=AsyncMock(),
        )
        gossip.add_seed_node("node-2", "localhost:5002")

        peers = gossip._select_peers()
        assert len(peers) == 1  # only 1 peer available

    async def test_fanout_excludes_self(self):
        """Peer selection should never include self."""
        gossip = GossipProtocol(
            node_id="node-1",
            address="localhost:5001",
            gossip_fanout=3,
            gossip_func=AsyncMock(),
        )
        gossip.add_seed_node("node-2", "localhost:5002")
        gossip.add_seed_node("node-3", "localhost:5003")

        for _ in range(20):  # run multiple times for randomness
            peers = gossip._select_peers()
            assert "node-1" not in peers

    async def test_fanout_excludes_down_nodes(self):
        """Peer selection should exclude DOWN nodes."""
        gossip = GossipProtocol(
            node_id="node-1",
            address="localhost:5001",
            gossip_fanout=3,
            gossip_func=AsyncMock(),
        )
        gossip.add_seed_node("node-2", "localhost:5002")
        gossip.add_seed_node("node-3", "localhost:5003")
        gossip._members["node-2"].status = NodeStatus.DOWN

        peers = gossip._select_peers()
        assert "node-2" not in peers


class TestGetAliveNodes:
    """Tests for get_alive_nodes."""

    async def test_get_alive_nodes_returns_only_alive(self, gossip):
        """get_alive_nodes should only return ALIVE nodes."""
        gossip.add_seed_node("node-2", "localhost:5002")
        gossip.add_seed_node("node-3", "localhost:5003")
        gossip._members["node-3"].status = NodeStatus.SUSPECTED

        alive = gossip.get_alive_nodes()
        alive_ids = [m.node_id for m in alive]
        assert "node-1" in alive_ids
        assert "node-2" in alive_ids
        assert "node-3" not in alive_ids


class TestGetNodeStatus:
    """Tests for get_node_status."""

    async def test_get_status_known_node(self, gossip):
        """Should return status for known nodes."""
        assert gossip.get_node_status("node-1") == NodeStatus.ALIVE

    async def test_get_status_unknown_node(self, gossip):
        """Should return None for unknown nodes."""
        assert gossip.get_node_status("unknown") is None


class TestAddSeedNode:
    """Tests for add_seed_node."""

    async def test_add_seed_node(self, gossip):
        """Should add a new seed node to membership."""
        gossip.add_seed_node("node-2", "localhost:5002")
        assert "node-2" in gossip.members
        assert gossip.members["node-2"].address == "localhost:5002"
        assert gossip.members["node-2"].status == NodeStatus.ALIVE

    async def test_add_seed_node_idempotent(self, gossip):
        """Adding the same seed node twice should not duplicate."""
        gossip.add_seed_node("node-2", "localhost:5002")
        gossip.add_seed_node("node-2", "localhost:5002")
        assert len([k for k in gossip.members if k == "node-2"]) == 1
