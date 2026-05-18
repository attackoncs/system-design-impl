"""Hinted handoff manager for handling temporarily unavailable nodes.

When a write cannot be delivered to its intended replica (because the node
is down), the hint is stored locally. A background task periodically
attempts to deliver stored hints to recovered nodes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Optional


@dataclass
class HintedData:
    """Data stored as a hint for a temporarily unavailable node.

    Attributes:
        target_node_id: The node this hint is intended for.
        key: The key that was written.
        value: The value bytes (None for deletes).
        timestamp: The write timestamp.
        source_node_id: The node that stored the hint.
    """

    target_node_id: str
    key: str
    value: Optional[bytes]
    timestamp: float
    source_node_id: str


# Type alias for the deliver function.
# It receives the target node_id and the list of hints to deliver.
DeliverFunc = Callable[[str, list[HintedData]], Awaitable[bool]]


class HintedHandoffManager:
    """Manages hinted handoff for temporarily unavailable nodes.

    Stores hints in memory keyed by target node. A background task
    periodically attempts to deliver hints to recovered nodes.

    Args:
        node_id: This node's identifier.
        deliver_func: Async callable to deliver hints to a target node.
            Returns True if delivery succeeded, False otherwise.
        handoff_interval: Seconds between handoff delivery attempts.
        is_node_alive_func: Optional callable to check if a node is alive.
    """

    def __init__(
        self,
        node_id: str,
        deliver_func: Optional[DeliverFunc] = None,
        handoff_interval: float = 10.0,
        is_node_alive_func: Optional[Callable[[str], bool]] = None,
    ):
        self._node_id = node_id
        self._deliver_func = deliver_func
        self._handoff_interval = handoff_interval
        self._is_node_alive_func = is_node_alive_func

        # In-memory hint storage: target_node_id -> list of hints
        self._hints: dict[str, list[HintedData]] = {}

        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    def pending_count(self) -> int:
        """Total number of pending hints across all target nodes."""
        return sum(len(hints) for hints in self._hints.values())

    async def start(self) -> None:
        """Start the background handoff delivery loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._handoff_loop())

    async def stop(self) -> None:
        """Stop the background handoff delivery loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def store_hint(self, hint: HintedData) -> None:
        """Store a hint for a temporarily unavailable node.

        Args:
            hint: The hinted data to store.
        """
        if hint.target_node_id not in self._hints:
            self._hints[hint.target_node_id] = []
        self._hints[hint.target_node_id].append(hint)

    def get_pending_hints(self, target_node_id: str) -> list[HintedData]:
        """Get all pending hints for a target node.

        Args:
            target_node_id: The node to get hints for.

        Returns:
            List of pending hints (empty if none).
        """
        return list(self._hints.get(target_node_id, []))

    async def deliver_hints(self, target_node_id: str) -> bool:
        """Attempt to deliver all hints for a target node.

        If delivery succeeds, the hints are removed from storage.

        Args:
            target_node_id: The node to deliver hints to.

        Returns:
            True if delivery succeeded (or no hints to deliver), False otherwise.
        """
        hints = self._hints.get(target_node_id)
        if not hints:
            return True

        if self._deliver_func is None:
            return False

        try:
            success = await self._deliver_func(target_node_id, hints)
            if success:
                # Remove delivered hints
                del self._hints[target_node_id]
            return success
        except Exception:
            return False

    async def _handoff_loop(self) -> None:
        """Background loop that attempts to deliver hints to recovered nodes."""
        while self._running:
            try:
                await self._attempt_deliveries()
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            await asyncio.sleep(self._handoff_interval)

    async def _attempt_deliveries(self) -> None:
        """Attempt to deliver hints to all target nodes that are alive."""
        # Get a snapshot of target nodes with pending hints
        targets = list(self._hints.keys())

        for target_node_id in targets:
            # Check if node is alive before attempting delivery
            if self._is_node_alive_func is not None:
                if not self._is_node_alive_func(target_node_id):
                    continue

            await self.deliver_hints(target_node_id)
