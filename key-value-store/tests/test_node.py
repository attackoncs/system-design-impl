"""Tests for the KVNode orchestrator.

Verifies that the node starts/stops all components in the correct order
and delegates put/get/delete operations to the RequestCoordinator.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from kv_store.config import NodeConfig
from kv_store.node import KVNode
from kv_store.replication.coordinator import PutResult, GetResult, DeleteResult
from kv_store.replication.vector_clock import VectorClock


@pytest.fixture
def config():
    """Create a default NodeConfig for testing."""
    return NodeConfig(node_id="test-node-1")


@pytest.fixture
def node_with_mocks(config):
    """Create a KVNode with all sub-components mocked."""
    with patch("kv_store.node.StorageEngine") as mock_storage_cls, \
         patch("kv_store.node.ConsistentHashRing") as mock_ring_cls, \
         patch("kv_store.node.GRPCClient") as mock_client_cls, \
         patch("kv_store.node.GossipProtocol") as mock_gossip_cls, \
         patch("kv_store.node.ClusterMembership") as mock_membership_cls, \
         patch("kv_store.node.HintedHandoffManager") as mock_handoff_cls, \
         patch("kv_store.node.MerkleTree") as mock_merkle_cls, \
         patch("kv_store.node.AntiEntropyManager") as mock_anti_entropy_cls, \
         patch("kv_store.node.QuorumManager") as mock_quorum_cls, \
         patch("kv_store.node.RequestCoordinator") as mock_coordinator_cls, \
         patch("kv_store.node.GRPCServer") as mock_server_cls:

        # Configure mock instances
        mock_storage = mock_storage_cls.return_value
        mock_storage.start = AsyncMock()
        mock_storage.stop = AsyncMock()

        mock_ring = mock_ring_cls.return_value

        mock_client = mock_client_cls.return_value

        mock_gossip = mock_gossip_cls.return_value

        mock_membership = mock_membership_cls.return_value
        mock_membership.join_cluster = AsyncMock()
        mock_membership.leave_cluster = AsyncMock()
        mock_membership.is_node_alive = MagicMock(return_value=True)

        mock_handoff = mock_handoff_cls.return_value
        mock_handoff.start = AsyncMock()
        mock_handoff.stop = AsyncMock()

        mock_merkle = mock_merkle_cls.return_value

        mock_anti_entropy = mock_anti_entropy_cls.return_value
        mock_anti_entropy.start = AsyncMock()
        mock_anti_entropy.stop = AsyncMock()

        mock_quorum = mock_quorum_cls.return_value

        mock_coordinator = mock_coordinator_cls.return_value
        mock_coordinator.put = AsyncMock()
        mock_coordinator.get = AsyncMock()
        mock_coordinator.delete = AsyncMock()

        mock_server = mock_server_cls.return_value
        mock_server.start = AsyncMock()
        mock_server.stop = AsyncMock()

        node = KVNode(config)

        yield {
            "node": node,
            "storage_engine": mock_storage,
            "hash_ring": mock_ring,
            "grpc_client": mock_client,
            "gossip": mock_gossip,
            "membership": mock_membership,
            "hinted_handoff": mock_handoff,
            "merkle_tree": mock_merkle,
            "anti_entropy": mock_anti_entropy,
            "quorum_manager": mock_quorum,
            "coordinator": mock_coordinator,
            "grpc_server": mock_server,
        }


class TestNodeStart:
    """Tests for KVNode.start() method."""

    @pytest.mark.asyncio
    async def test_start_calls_components_in_correct_order(self, node_with_mocks):
        """Node starts all components in the specified order."""
        mocks = node_with_mocks
        node = mocks["node"]

        await node.start()

        # Verify all components were started
        mocks["storage_engine"].start.assert_called_once()
        mocks["grpc_server"].start.assert_called_once()
        mocks["membership"].join_cluster.assert_called_once()
        mocks["hinted_handoff"].start.assert_called_once()
        mocks["anti_entropy"].start.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_order_storage_before_grpc(self, node_with_mocks):
        """Storage engine starts before gRPC server."""
        mocks = node_with_mocks
        node = mocks["node"]

        call_order = []
        mocks["storage_engine"].start.side_effect = lambda: call_order.append("storage")
        mocks["grpc_server"].start.side_effect = lambda: call_order.append("grpc_server")
        mocks["membership"].join_cluster.side_effect = lambda *a, **kw: call_order.append("membership")
        mocks["hinted_handoff"].start.side_effect = lambda: call_order.append("hinted_handoff")
        mocks["anti_entropy"].start.side_effect = lambda: call_order.append("anti_entropy")

        await node.start()

        assert call_order == [
            "storage",
            "grpc_server",
            "membership",
            "hinted_handoff",
            "anti_entropy",
        ]

    @pytest.mark.asyncio
    async def test_start_sets_running_flag(self, node_with_mocks):
        """Node sets is_running to True after successful start."""
        node = node_with_mocks["node"]

        assert not node.is_running
        await node.start()
        assert node.is_running

    @pytest.mark.asyncio
    async def test_start_raises_if_already_running(self, node_with_mocks):
        """Node raises RuntimeError if start() called when already running."""
        node = node_with_mocks["node"]

        await node.start()
        with pytest.raises(RuntimeError, match="already running"):
            await node.start()


class TestNodeStop:
    """Tests for KVNode.stop() method."""

    @pytest.mark.asyncio
    async def test_stop_calls_components_in_correct_order(self, node_with_mocks):
        """Node stops all components in the specified order."""
        mocks = node_with_mocks
        node = mocks["node"]

        await node.start()

        call_order = []
        mocks["anti_entropy"].stop.side_effect = lambda: call_order.append("anti_entropy")
        mocks["hinted_handoff"].stop.side_effect = lambda: call_order.append("hinted_handoff")
        mocks["membership"].leave_cluster.side_effect = lambda: call_order.append("membership")
        mocks["grpc_server"].stop.side_effect = lambda: call_order.append("grpc_server")
        mocks["storage_engine"].stop.side_effect = lambda: call_order.append("storage")

        await node.stop()

        assert call_order == [
            "anti_entropy",
            "hinted_handoff",
            "membership",
            "grpc_server",
            "storage",
        ]

    @pytest.mark.asyncio
    async def test_stop_clears_running_flag(self, node_with_mocks):
        """Node sets is_running to False after successful stop."""
        node = node_with_mocks["node"]

        await node.start()
        assert node.is_running
        await node.stop()
        assert not node.is_running

    @pytest.mark.asyncio
    async def test_stop_raises_if_not_running(self, node_with_mocks):
        """Node raises RuntimeError if stop() called when not running."""
        node = node_with_mocks["node"]

        with pytest.raises(RuntimeError, match="not running"):
            await node.stop()


class TestNodeDelegation:
    """Tests for put/get/delete delegation to RequestCoordinator."""

    @pytest.mark.asyncio
    async def test_put_delegates_to_coordinator(self, node_with_mocks):
        """put() delegates to RequestCoordinator.put()."""
        mocks = node_with_mocks
        node = mocks["node"]
        coordinator = mocks["coordinator"]

        expected_result = PutResult(
            success=True,
            vector_clock=VectorClock({"test-node-1": 1}),
            replicas_acknowledged=2,
        )
        coordinator.put.return_value = expected_result

        clock = VectorClock({"test-node-1": 0})
        result = await node.put("key1", b"value1", clock, None)

        coordinator.put.assert_called_once_with("key1", b"value1", clock, None)
        assert result == expected_result

    @pytest.mark.asyncio
    async def test_get_delegates_to_coordinator(self, node_with_mocks):
        """get() delegates to RequestCoordinator.get()."""
        mocks = node_with_mocks
        node = mocks["node"]
        coordinator = mocks["coordinator"]

        expected_result = GetResult(
            found=True,
            value=b"value1",
            vector_clock=VectorClock({"test-node-1": 1}),
            has_conflict=False,
            conflicting_values=[],
        )
        coordinator.get.return_value = expected_result

        result = await node.get("key1", None)

        coordinator.get.assert_called_once_with("key1", None)
        assert result == expected_result

    @pytest.mark.asyncio
    async def test_delete_delegates_to_coordinator(self, node_with_mocks):
        """delete() delegates to RequestCoordinator.delete()."""
        mocks = node_with_mocks
        node = mocks["node"]
        coordinator = mocks["coordinator"]

        expected_result = DeleteResult(
            success=True,
            vector_clock=VectorClock({"test-node-1": 2}),
        )
        coordinator.delete.return_value = expected_result

        clock = VectorClock({"test-node-1": 1})
        result = await node.delete("key1", clock, None)

        coordinator.delete.assert_called_once_with("key1", clock, None)
        assert result == expected_result


class TestNodeStartupFailure:
    """Tests for graceful handling of startup failures."""

    @pytest.mark.asyncio
    async def test_storage_failure_prevents_start(self, node_with_mocks):
        """If storage engine fails to start, node does not start."""
        mocks = node_with_mocks
        node = mocks["node"]

        mocks["storage_engine"].start.side_effect = RuntimeError("disk error")

        with pytest.raises(RuntimeError, match="disk error"):
            await node.start()

        assert not node.is_running

    @pytest.mark.asyncio
    async def test_grpc_server_failure_cleans_up(self, node_with_mocks):
        """If gRPC server fails, storage engine is cleaned up."""
        mocks = node_with_mocks
        node = mocks["node"]

        mocks["grpc_server"].start.side_effect = RuntimeError("port in use")

        with pytest.raises(RuntimeError, match="port in use"):
            await node.start()

        assert not node.is_running
        # Storage should have been cleaned up
        mocks["storage_engine"].stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_membership_failure_cleans_up(self, node_with_mocks):
        """If cluster join fails, earlier components are cleaned up."""
        mocks = node_with_mocks
        node = mocks["node"]

        mocks["membership"].join_cluster.side_effect = RuntimeError("no seeds")

        with pytest.raises(RuntimeError, match="no seeds"):
            await node.start()

        assert not node.is_running
        # Both gRPC server and storage should be cleaned up
        mocks["grpc_server"].stop.assert_called_once()
        mocks["storage_engine"].stop.assert_called_once()
