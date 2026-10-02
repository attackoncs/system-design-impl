"""Per-channel message queue system using asyncio.Queue.

Maintains separate asyncio.Queue instances for each notification
channel. Supports configurable max depth per channel and provides
depth/pending count queries.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from notification_system.exceptions import QueueFullError
from notification_system.models import Channel, NotificationTask


class MessageQueue:
    """Per-channel message queue system using asyncio.Queue.

    Maintains separate asyncio.Queue instances for each notification
    channel. Supports configurable max depth per channel and provides
    depth/pending count queries.
    """

    def __init__(self, max_depth: int = 10000) -> None:
        self._max_depth = max_depth
        self._queues: dict[Channel, asyncio.Queue[NotificationTask]] = {
            channel: asyncio.Queue(maxsize=max_depth)
            for channel in Channel
        }

    async def enqueue(self, task: NotificationTask) -> None:
        """Enqueue a notification task to the appropriate channel queue.

        Args:
            task: The notification task to enqueue.

        Raises:
            QueueFullError: If the channel queue is at max capacity.
        """
        queue = self._queues[task.channel]
        if queue.full():
            raise QueueFullError(task.channel.value, self._max_depth)
        await queue.put(task)

    async def dequeue(self, channel: Channel) -> NotificationTask:
        """Dequeue the next task from a channel queue (blocks if empty).

        Args:
            channel: The channel to dequeue from.

        Returns:
            The next NotificationTask in FIFO order.
        """
        return await self._queues[channel].get()

    def dequeue_nowait(self, channel: Channel) -> Optional[NotificationTask]:
        """Non-blocking dequeue. Returns None if queue is empty."""
        try:
            return self._queues[channel].get_nowait()
        except asyncio.QueueEmpty:
            return None

    def depth(self, channel: Channel) -> int:
        """Current number of pending tasks in a channel queue."""
        return self._queues[channel].qsize()

    def is_full(self, channel: Channel) -> bool:
        """Check if a channel queue is at max capacity."""
        return self._queues[channel].full()

    def is_empty(self, channel: Channel) -> bool:
        """Check if a channel queue has no pending tasks."""
        return self._queues[channel].empty()
