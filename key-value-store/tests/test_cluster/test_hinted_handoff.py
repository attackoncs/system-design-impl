"""Tests for the hinted handoff manager."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from kv_store.cluster.hinted_handoff import HintedData, HintedHandoffManager


@pytest.fixture
def deliver_func():
    """Create a mock deliver function that succeeds."""
    mock = AsyncMock(return_value=True)
    return mock


@pytest.fixture
def manager(deliver_func):
    """Create a HintedHandoffManager instance."""
    return HintedHandoffManager(
        node_id="node-1",
        deliver_func=deliver_func,
        handoff_interval=1.0,
        is_node_alive_func=lambda node_id: True,
    )


def make_hint(target: str = "node-2", key: str = "key1") -> HintedData:
    """Helper to create a HintedData instance."""
    return HintedData(
        target_node_id=target,
        key=key,
        value=b"value1",
        timestamp=1000.0,
        source_node_id="node-1",
    )


class TestStoreAndRetrieve:
    """Tests for storing and retrieving hints."""

    async def test_store_and_get_hints(self, manager):
        """Should store and retrieve hints for a target node."""
        hint = make_hint()
        manager.store_hint(hint)

        pending = manager.get_pending_hints("node-2")
        assert len(pending) == 1
        assert pending[0].key == "key1"
        assert pending[0].target_node_id == "node-2"

    async def test_store_multiple_hints(self, manager):
        """Should store multiple hints for the same target."""
        manager.store_hint(make_hint(key="key1"))
        manager.store_hint(make_hint(key="key2"))
        manager.store_hint(make_hint(key="key3"))

        pending = manager.get_pending_hints("node-2")
        assert len(pending) == 3

    async def test_get_hints_empty(self, manager):
        """Should return empty list for node with no hints."""
        pending = manager.get_pending_hints("node-99")
        assert pending == []

    async def test_store_hints_different_targets(self, manager):
        """Should store hints separately per target node."""
        manager.store_hint(make_hint(target="node-2", key="key1"))
        manager.store_hint(make_hint(target="node-3", key="key2"))

        assert len(manager.get_pending_hints("node-2")) == 1
        assert len(manager.get_pending_hints("node-3")) == 1


class TestDeliverHints:
    """Tests for hint delivery."""

    async def test_deliver_hints_success(self, manager, deliver_func):
        """Successful delivery should remove hints."""
        manager.store_hint(make_hint())
        assert manager.pending_count == 1

        result = await manager.deliver_hints("node-2")
        assert result is True
        assert manager.pending_count == 0
        deliver_func.assert_called_once()

    async def test_deliver_hints_failure(self, manager, deliver_func):
        """Failed delivery should keep hints."""
        deliver_func.return_value = False
        manager.store_hint(make_hint())

        result = await manager.deliver_hints("node-2")
        assert result is False
        assert manager.pending_count == 1

    async def test_deliver_no_hints(self, manager, deliver_func):
        """Delivering to node with no hints should return True."""
        result = await manager.deliver_hints("node-2")
        assert result is True
        deliver_func.assert_not_called()

    async def test_hints_deleted_after_delivery(self, manager, deliver_func):
        """Hints should be completely removed after successful delivery."""
        manager.store_hint(make_hint(key="key1"))
        manager.store_hint(make_hint(key="key2"))

        await manager.deliver_hints("node-2")
        assert manager.get_pending_hints("node-2") == []
        assert manager.pending_count == 0


class TestPendingCount:
    """Tests for pending_count property."""

    async def test_pending_count_empty(self, manager):
        """Should be 0 when no hints stored."""
        assert manager.pending_count == 0

    async def test_pending_count_tracks_correctly(self, manager):
        """Should track total hints across all targets."""
        manager.store_hint(make_hint(target="node-2", key="key1"))
        manager.store_hint(make_hint(target="node-2", key="key2"))
        manager.store_hint(make_hint(target="node-3", key="key3"))

        assert manager.pending_count == 3

    async def test_pending_count_decreases_after_delivery(self, manager):
        """Should decrease after successful delivery."""
        manager.store_hint(make_hint(target="node-2", key="key1"))
        manager.store_hint(make_hint(target="node-3", key="key2"))

        await manager.deliver_hints("node-2")
        assert manager.pending_count == 1


class TestHandoffLoop:
    """Tests for the background handoff loop."""

    async def test_handoff_loop_delivers_to_alive_nodes(self, deliver_func):
        """Handoff loop should attempt delivery for alive nodes."""
        alive_nodes = {"node-2"}
        manager = HintedHandoffManager(
            node_id="node-1",
            deliver_func=deliver_func,
            handoff_interval=0.1,
            is_node_alive_func=lambda nid: nid in alive_nodes,
        )
        manager.store_hint(make_hint(target="node-2"))
        manager.store_hint(make_hint(target="node-3"))  # node-3 not alive

        await manager._attempt_deliveries()

        # Only node-2 hints should be delivered
        assert manager.pending_count == 1  # node-3 hints remain
        assert manager.get_pending_hints("node-2") == []

    async def test_handoff_loop_skips_dead_nodes(self, deliver_func):
        """Handoff loop should skip nodes that are not alive."""
        manager = HintedHandoffManager(
            node_id="node-1",
            deliver_func=deliver_func,
            handoff_interval=0.1,
            is_node_alive_func=lambda nid: False,  # all nodes dead
        )
        manager.store_hint(make_hint(target="node-2"))

        await manager._attempt_deliveries()

        # Hints should remain since node is not alive
        assert manager.pending_count == 1
        deliver_func.assert_not_called()
