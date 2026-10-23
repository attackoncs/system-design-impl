"""Tests for the KVNode orchestrator.

Verifies that the node starts/stops all components in the correct order and
delegates put/get/delete operations to the RequestCoordinator.
"""

import unittest.mock

import kv_store.config
import kv_store.node
import kv_store.replication.coordinator
import kv_store.replication.vector_clock
import pytest


@pytest.fixture
def config():
    """Create a default NodeConfig for testing."""
    return kv_store.config.NodeConfig(node_id="test-node-1")


@pytest.fixture
def node_with_mocks(config):
    """Create a KVNode with all sub-components mocked."""
    with unittest.mock.patch(
        "kv_store.node.storage_engine.StorageEngine"
    ) as mock_storage_cls, unittest.mock.patch(
        "kv_store.node.ConsistentHashRing"
    ) as mock_ring_cls, unittest.mock.patch(
        "kv_store.node.network_grpc_client.GRPCClient"
    ) as mock_client_cls, unittest.mock.patch(
        "kv_store.node.cluster_gossip.GossipProtocol"
    ) as mock_gossip_cls, unittest.mock.patch(
        "kv_store.node.cluster_membership.ClusterMembership"
    ) as mock_membership_cls, unittest.mock.patch(
        "kv_store.node.cluster_hinted_handoff.HintedHandoffManager"
    ) as mock_handoff_cls, unittest.mock.patch(
        "kv_store.node.cluster_merkle_tree.MerkleTree"
    ) as mock_merkle_cls, unittest.mock.patch(
        "kv_store.node.cluster_merkle_tree.AntiEntropyManager"
    ) as mock_anti_entropy_cls, unittest.mock.patch(
        "kv_store.node.replication_quorum.QuorumManager"
    ) as mock_quorum_cls, unittest.mock.patch(
        "kv_store.node.replication_coordinator.RequestCoordinator"
    ) as mock_coordinator_cls, unittest.mock.patch(
        "kv_store.node.network_grpc_server.GRPCServer"
    ) as mock_server_cls:

        # Configure mock instances
        mock_storage = mock_storage_cls.return_value
        mock_storage.start = unittest.mock.AsyncMock()
        mock_storage.stop = unittest.mock.AsyncMock()
        mock_storage.scan = unittest.mock.AsyncMock(return_value={})

        mock_ring = mock_ring_cls.return_value

        mock_client = mock_client_cls.return_value
        mock_client.close = unittest.mock.AsyncMock()

        mock_gossip = mock_gossip_cls.return_value

        mock_membership = mock_membership_cls.return_value
        mock_membership.join_cluster = unittest.mock.AsyncMock()
        mock_membership.leave_cluster = unittest.mock.AsyncMock()
        mock_membership.is_node_alive = unittest.mock.MagicMock(return_value=True)

        mock_handoff = mock_handoff_cls.return_value
        mock_handoff.start = unittest.mock.AsyncMock()
        mock_handoff.stop = unittest.mock.AsyncMock()

        mock_merkle = mock_merkle_cls.return_value

        mock_anti_entropy = mock_anti_entropy_cls.return_value
        mock_anti_entropy.start = unittest.mock.AsyncMock()
        mock_anti_entropy.stop = unittest.mock.AsyncMock()

        mock_quorum = mock_quorum_cls.return_value

        mock_coordinator = mock_coordinator_cls.return_value
        mock_coordinator.close = unittest.mock.AsyncMock()
        mock_coordinator.put = unittest.mock.AsyncMock()
        mock_coordinator.get = unittest.mock.AsyncMock()
        mock_coordinator.delete = unittest.mock.AsyncMock()

        mock_server = mock_server_cls.return_value
        mock_server.start = unittest.mock.AsyncMock()
        mock_server.stop = unittest.mock.AsyncMock()

        node = kv_store.node.KVNode(config)

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
    async def test_start_calls_components_in_correct_order(
        self, node_with_mocks
    ):
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
        mocks["storage_engine"].start.side_effect = lambda: call_order.append(
            "storage"
        )
        mocks["grpc_server"].start.side_effect = lambda: call_order.append(
            "grpc_server"
        )
        mocks["membership"].join_cluster.side_effect = (
            lambda *a, **kw: call_order.append("membership")
        )
        mocks["hinted_handoff"].start.side_effect = lambda: call_order.append(
            "hinted_handoff"
        )
        mocks["anti_entropy"].start.side_effect = lambda: call_order.append(
            "anti_entropy"
        )

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
    async def test_stop_calls_components_in_correct_order(
        self, node_with_mocks
    ):
        """Node stops all components in the specified order."""
        mocks = node_with_mocks
        node = mocks["node"]

        await node.start()

        call_order = []
        mocks["anti_entropy"].stop.side_effect = lambda: call_order.append(
            "anti_entropy"
        )
        mocks["hinted_handoff"].stop.side_effect = lambda: call_order.append(
            "hinted_handoff"
        )
        mocks["membership"].leave_cluster.side_effect = (
            lambda: call_order.append("membership")
        )
        mocks["grpc_server"].stop.side_effect = lambda: call_order.append(
            "grpc_server"
        )
        mocks["storage_engine"].stop.side_effect = lambda: call_order.append(
            "storage"
        )

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
        """Put() delegates to RequestCoordinator.put()."""
        mocks = node_with_mocks
        node = mocks["node"]
        coordinator = mocks["coordinator"]

        expected_result = kv_store.replication.coordinator.PutResult(
            success=True,
            vector_clock=kv_store.replication.vector_clock.VectorClock({"test-node-1": 1}),
            replicas_acknowledged=2,
        )
        coordinator.put.return_value = expected_result

        clock = kv_store.replication.vector_clock.VectorClock({"test-node-1": 0})
        result = await node.put("key1", b"value1", clock, None)

        coordinator.put.assert_called_once_with("key1", b"value1", clock, None)
        assert result == expected_result

    @pytest.mark.asyncio
    async def test_get_delegates_to_coordinator(self, node_with_mocks):
        """Get() delegates to RequestCoordinator.get()."""
        mocks = node_with_mocks
        node = mocks["node"]
        coordinator = mocks["coordinator"]

        expected_result = kv_store.replication.coordinator.GetResult(
            found=True,
            value=b"value1",
            vector_clock=kv_store.replication.vector_clock.VectorClock({"test-node-1": 1}),
            has_conflict=False,
            conflicting_values=[],
        )
        coordinator.get.return_value = expected_result

        result = await node.get("key1", None)

        coordinator.get.assert_called_once_with("key1", None)
        assert result == expected_result

    @pytest.mark.asyncio
    async def test_delete_delegates_to_coordinator(self, node_with_mocks):
        """Delete() delegates to RequestCoordinator.delete()."""
        mocks = node_with_mocks
        node = mocks["node"]
        coordinator = mocks["coordinator"]

        expected_result = kv_store.replication.coordinator.DeleteResult(
            success=True,
            vector_clock=kv_store.replication.vector_clock.VectorClock({"test-node-1": 2}),
        )
        coordinator.delete.return_value = expected_result

        clock = kv_store.replication.vector_clock.VectorClock({"test-node-1": 1})
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
