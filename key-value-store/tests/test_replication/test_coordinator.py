"""Tests for the Request Coordinator.

Tests cover:
- Put writes to N replicas and returns after W acks
- Get reads from R replicas and returns newest value
- Conflict detection returns all versions to client
- Sloppy quorum substitutes unavailable nodes
- Hinted handoff triggered for failed nodes
- Delete writes tombstone

All dependencies (hash_ring, quorum_manager, grpc_client, storage_engine,
membership, hinted_handoff) are mocked.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from kv_store.replication.coordinator import (
    RequestCoordinator,
    PutResult,
    GetResult,
    DeleteResult,
    QuorumNotMetError,
)
from kv_store.replication.vector_clock import VectorClock
from kv_store.replication.quorum import (
    ConsistencyLevel,
    QuorumConfig,
    QuorumManager,
)
from kv_store.config import ReplicationConfig


@pytest.fixture
def replication_config():
    """Default replication config: N=3, W=2, R=2."""
    return ReplicationConfig(n_replicas=3, w_quorum=2, r_quorum=2)


@pytest.fixture
def quorum_manager(replication_config):
    """Real QuorumManager with default config."""
    return QuorumManager(replication_config)


@pytest.fixture
def mock_hash_ring():
    """Mock hash ring that returns predictable node lists."""
    ring = MagicMock()
    ring.get_nodes.return_value = ["node-1", "node-2", "node-3"]
    return ring


@pytest.fixture
def mock_grpc_client():
    """Mock gRPC client for inter-node communication."""
    client = MagicMock()
    client.put = AsyncMock(return_value=True)
    client.get = AsyncMock(return_value=None)
    return client


@pytest.fixture
def mock_storage_engine():
    """Mock local storage engine."""
    engine = MagicMock()
    engine.put = AsyncMock(return_value=None)
    engine.get = AsyncMock(return_value=None)
    engine.delete = AsyncMock(return_value=None)
    return engine


@pytest.fixture
def mock_membership():
    """Mock cluster membership where all nodes are alive."""
    membership = MagicMock()
    membership.is_node_alive.return_value = True
    membership.get_alive_members.return_value = ["node-1", "node-2", "node-3"]
    return membership


@pytest.fixture
def mock_hinted_handoff():
    """Mock hinted handoff manager."""
    handoff = MagicMock()
    handoff.store_hint = MagicMock()
    return handoff


@pytest.fixture
def coordinator(
    mock_hash_ring,
    quorum_manager,
    mock_grpc_client,
    mock_storage_engine,
    mock_membership,
    mock_hinted_handoff,
):
    """Request coordinator with node-1 as the local node."""
    return RequestCoordinator(
        node_id="node-1",
        hash_ring=mock_hash_ring,
        quorum_manager=quorum_manager,
        grpc_client=mock_grpc_client,
        storage_engine=mock_storage_engine,
        membership=mock_membership,
        hinted_handoff=mock_hinted_handoff,
    )


class TestPut:
    """Tests for the put operation."""

    async def test_put_writes_to_n_replicas_returns_after_w_acks(
        self, coordinator, mock_storage_engine, mock_grpc_client
    ):
        """Put writes to all N=3 replicas and succeeds with W=2 acks."""
        result = await coordinator.put("key1", b"value1")

        assert result.success is True
        assert result.replicas_acknowledged == 3
        assert isinstance(result.vector_clock, VectorClock)
        # Local write (node-1 is a replica)
        mock_storage_engine.put.assert_called_once()
        # Remote writes to node-2 and node-3
        assert mock_grpc_client.put.call_count == 2

    async def test_put_increments_vector_clock(self, coordinator):
        """Put increments the coordinator's entry in the vector clock."""
        result = await coordinator.put("key1", b"value1")

        clock_dict = result.vector_clock.to_dict()
        assert "node-1" in clock_dict
        assert clock_dict["node-1"] == 1

    async def test_put_with_client_clock(self, coordinator):
        """Put uses client-provided vector clock as base."""
        client_clock = VectorClock({"node-2": 3, "node-1": 1})
        result = await coordinator.put("key1", b"value1", client_clock=client_clock)

        clock_dict = result.vector_clock.to_dict()
        assert clock_dict["node-1"] == 2  # incremented from 1
        assert clock_dict["node-2"] == 3  # preserved

    async def test_put_succeeds_with_exactly_w_acks(
        self, coordinator, mock_grpc_client
    ):
        """Put succeeds when exactly W=2 nodes acknowledge."""
        # node-2 succeeds, node-3 fails
        mock_grpc_client.put.side_effect = [True, Exception("connection failed")]

        result = await coordinator.put("key1", b"value1")

        # node-1 (local) + node-2 = 2 acks (meets W=2)
        assert result.success is True
        assert result.replicas_acknowledged == 2

    async def test_put_raises_quorum_not_met_when_insufficient_acks(
        self, coordinator, mock_grpc_client, mock_storage_engine
    ):
        """Put raises QuorumNotMetError when fewer than W nodes acknowledge."""
        # Both remote nodes fail, and local also fails
        mock_grpc_client.put.side_effect = [
            Exception("connection failed"),
            Exception("connection failed"),
        ]
        mock_storage_engine.put.side_effect = Exception("disk error")

        with pytest.raises(QuorumNotMetError) as exc_info:
            await coordinator.put("key1", b"value1")

        assert exc_info.value.required == 2
        assert exc_info.value.received == 0

    async def test_put_validates_key_size(self, coordinator):
        """Put raises ValueError for keys exceeding 256 bytes."""
        long_key = "k" * 257
        with pytest.raises(ValueError, match="Key exceeds maximum size"):
            await coordinator.put(long_key, b"value")

    async def test_put_validates_value_size(self, coordinator):
        """Put raises ValueError for values exceeding 10 KB."""
        large_value = b"x" * (10 * 1024 + 1)
        with pytest.raises(ValueError, match="Value exceeds maximum size"):
            await coordinator.put("key1", large_value)

    async def test_put_with_consistency_level(
        self, coordinator, mock_hash_ring, mock_grpc_client
    ):
        """Put respects the consistency level override."""
        # With ConsistencyLevel.ONE, only 1 ack needed
        # Even if one remote fails, should still succeed
        mock_grpc_client.put.side_effect = [Exception("fail"), True]

        result = await coordinator.put(
            "key1", b"value1", consistency=ConsistencyLevel.ONE
        )

        assert result.success is True


class TestGet:
    """Tests for the get operation."""

    async def test_get_reads_from_r_replicas_returns_newest_value(
        self, coordinator, mock_storage_engine, mock_grpc_client
    ):
        """Get queries replicas and returns the value with the newest clock."""
        # Local storage returns a value
        local_result = MagicMock()
        local_result.found = True
        local_result.is_tombstone = False
        local_result.value = b"value_v1"
        mock_storage_engine.get.return_value = local_result

        # Remote nodes return newer value
        newer_clock = VectorClock({"node-2": 2})
        mock_grpc_client.get.return_value = (b"value_v2", newer_clock)

        result = await coordinator.get("key1")

        assert result.found is True
        assert result.value == b"value_v2"
        assert result.has_conflict is False

    async def test_get_returns_not_found_when_no_values(
        self, coordinator, mock_storage_engine, mock_grpc_client
    ):
        """Get returns found=False when key doesn't exist on any replica."""
        mock_storage_engine.get.return_value = None
        mock_grpc_client.get.return_value = None

        result = await coordinator.get("nonexistent")

        assert result.found is False
        assert result.value is None

    async def test_get_conflict_detection_returns_all_versions(
        self, coordinator, mock_storage_engine, mock_grpc_client
    ):
        """Get detects concurrent versions and returns all conflicting values."""
        # Local storage has no value
        mock_storage_engine.get.return_value = None

        # Two remote nodes return concurrent versions (neither dominates)
        clock_a = VectorClock({"node-1": 2, "node-2": 1})
        clock_b = VectorClock({"node-1": 1, "node-2": 2})
        mock_grpc_client.get.side_effect = [
            (b"value_a", clock_a),
            (b"value_b", clock_b),
        ]

        result = await coordinator.get("key1")

        assert result.found is True
        assert result.has_conflict is True
        assert len(result.conflicting_values) == 2
        # Both values should be present
        conflict_values = [v for v, _ in result.conflicting_values]
        assert b"value_a" in conflict_values
        assert b"value_b" in conflict_values

    async def test_get_resolves_dominated_version(
        self, coordinator, mock_storage_engine, mock_grpc_client
    ):
        """Get resolves when one version dominates another."""
        mock_storage_engine.get.return_value = None

        # node-2 has older version, node-3 has newer version
        old_clock = VectorClock({"node-1": 1})
        new_clock = VectorClock({"node-1": 2, "node-2": 1})
        mock_grpc_client.get.side_effect = [
            (b"old_value", old_clock),
            (b"new_value", new_clock),
        ]

        result = await coordinator.get("key1")

        assert result.found is True
        assert result.has_conflict is False
        assert result.value == b"new_value"
        assert result.vector_clock == new_clock

    async def test_get_raises_quorum_not_met_when_insufficient_responses(
        self, coordinator, mock_storage_engine, mock_grpc_client
    ):
        """Get raises QuorumNotMetError when fewer than R nodes respond."""
        # Local read fails
        mock_storage_engine.get.side_effect = Exception("disk error")
        # Both remote reads fail
        mock_grpc_client.get.side_effect = [
            Exception("connection failed"),
            Exception("connection failed"),
        ]

        with pytest.raises(QuorumNotMetError) as exc_info:
            await coordinator.get("key1")

        assert exc_info.value.required == 2
        assert exc_info.value.received == 0


class TestDelete:
    """Tests for the delete operation."""

    async def test_delete_writes_tombstone(
        self, coordinator, mock_storage_engine, mock_grpc_client
    ):
        """Delete writes a tombstone to replicas via the put path."""
        result = await coordinator.delete("key1")

        assert result.success is True
        assert isinstance(result.vector_clock, VectorClock)
        # Local delete (tombstone)
        mock_storage_engine.delete.assert_called_once()
        # Remote tombstone writes
        assert mock_grpc_client.put.call_count == 2

    async def test_delete_increments_vector_clock(self, coordinator):
        """Delete increments the coordinator's vector clock entry."""
        result = await coordinator.delete("key1")

        clock_dict = result.vector_clock.to_dict()
        assert "node-1" in clock_dict
        assert clock_dict["node-1"] == 1

    async def test_delete_with_client_clock(self, coordinator):
        """Delete uses client-provided vector clock as base."""
        client_clock = VectorClock({"node-2": 5})
        result = await coordinator.delete("key1", client_clock=client_clock)

        clock_dict = result.vector_clock.to_dict()
        assert clock_dict["node-1"] == 1
        assert clock_dict["node-2"] == 5

    async def test_delete_raises_quorum_not_met(
        self, coordinator, mock_storage_engine, mock_grpc_client
    ):
        """Delete raises QuorumNotMetError when quorum cannot be met."""
        mock_storage_engine.delete.side_effect = Exception("disk error")
        mock_grpc_client.put.side_effect = [
            Exception("fail"),
            Exception("fail"),
        ]

        with pytest.raises(QuorumNotMetError):
            await coordinator.delete("key1")


class TestSloppyQuorum:
    """Tests for sloppy quorum behavior."""

    async def test_sloppy_quorum_substitutes_unavailable_nodes(
        self,
        mock_hash_ring,
        quorum_manager,
        mock_grpc_client,
        mock_storage_engine,
        mock_membership,
        mock_hinted_handoff,
    ):
        """When a replica is unavailable, a substitute node is used."""
        # node-3 is down
        mock_membership.is_node_alive.side_effect = lambda nid: nid != "node-3"
        # Provide a substitute node
        mock_membership.get_alive_members.return_value = [
            "node-1",
            "node-2",
            "node-4",
        ]

        coordinator = RequestCoordinator(
            node_id="node-1",
            hash_ring=mock_hash_ring,
            quorum_manager=quorum_manager,
            grpc_client=mock_grpc_client,
            storage_engine=mock_storage_engine,
            membership=mock_membership,
            hinted_handoff=mock_hinted_handoff,
        )

        result = await coordinator.put("key1", b"value1")

        assert result.success is True
        # Verify that node-4 was used as substitute (gRPC called for node-2 and node-4)
        call_targets = [
            call.args[0] for call in mock_grpc_client.put.call_args_list
        ]
        assert "node-4" in call_targets
        assert "node-3" not in call_targets

    async def test_sloppy_quorum_no_substitute_available(
        self,
        mock_hash_ring,
        quorum_manager,
        mock_grpc_client,
        mock_storage_engine,
        mock_membership,
        mock_hinted_handoff,
    ):
        """When no substitute is available, the original node is kept."""
        # node-3 is down and no other nodes available
        mock_membership.is_node_alive.side_effect = lambda nid: nid != "node-3"
        mock_membership.get_alive_members.return_value = ["node-1", "node-2"]

        coordinator = RequestCoordinator(
            node_id="node-1",
            hash_ring=mock_hash_ring,
            quorum_manager=quorum_manager,
            grpc_client=mock_grpc_client,
            storage_engine=mock_storage_engine,
            membership=mock_membership,
            hinted_handoff=mock_hinted_handoff,
        )

        # The write to node-3 will fail, triggering hinted handoff
        mock_grpc_client.put.side_effect = [True, Exception("node-3 is down")]

        result = await coordinator.put("key1", b"value1")

        # Still succeeds because node-1 (local) + node-2 = 2 acks (W=2)
        assert result.success is True
        assert result.replicas_acknowledged == 2


class TestHintedHandoff:
    """Tests for hinted handoff integration."""

    async def test_hinted_handoff_triggered_for_failed_nodes(
        self, coordinator, mock_grpc_client, mock_hinted_handoff
    ):
        """When a replica write fails, a hint is stored for later delivery."""
        # node-3 fails
        mock_grpc_client.put.side_effect = [True, Exception("connection refused")]

        result = await coordinator.put("key1", b"value1")

        assert result.success is True
        # Hint should be stored for the failed node
        mock_hinted_handoff.store_hint.assert_called_once()
        call_args = mock_hinted_handoff.store_hint.call_args
        assert call_args[0][0] == "node-3"  # target node
        assert call_args[0][1] == "key1"  # key
        assert call_args[0][2] == b"value1"  # value

    async def test_hinted_handoff_triggered_for_delete_failures(
        self, coordinator, mock_grpc_client, mock_hinted_handoff
    ):
        """When a replica delete fails, a hint is stored."""
        mock_grpc_client.put.side_effect = [True, Exception("connection refused")]

        result = await coordinator.delete("key1")

        assert result.success is True
        mock_hinted_handoff.store_hint.assert_called_once()
        call_args = mock_hinted_handoff.store_hint.call_args
        assert call_args[0][0] == "node-3"  # target node
        assert call_args[0][1] == "key1"  # key
        assert call_args[0][2] == b""  # tombstone value

    async def test_no_hint_stored_when_all_writes_succeed(
        self, coordinator, mock_hinted_handoff
    ):
        """No hints are stored when all replica writes succeed."""
        result = await coordinator.put("key1", b"value1")

        assert result.success is True
        mock_hinted_handoff.store_hint.assert_not_called()
