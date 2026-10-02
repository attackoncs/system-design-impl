"""Message queue for async fanout task processing."""

from __future__ import annotations

import asyncio

from news_feed_system.models import FanoutTask


class MessageQueue:
    """Async message queue wrapping asyncio.Queue for fanout task buffering.

    Provides FIFO ordering with configurable max size. Used to decouple
    post publishing from fanout distribution to followers.
    """

    def __init__(self, max_size: int = 0) -> None:
        """Initialize the message queue.

        Args:
            max_size: Maximum number of items the queue can hold.
                      0 means unlimited.
        """
        self._queue: asyncio.Queue[FanoutTask] = asyncio.Queue(maxsize=max_size)
        self._max_size = max_size

    async def enqueue(self, task: FanoutTask) -> None:
        """Add a fanout task to the queue.

        Blocks if the queue is full until space becomes available.

        Args:
            task: The FanoutTask to enqueue.
        """
        await self._queue.put(task)

    async def dequeue(self) -> FanoutTask:
        """Remove and return the next fanout task from the queue.

        Blocks if the queue is empty until an item becomes available.

        Returns:
            The next FanoutTask in FIFO order.
        """
        return await self._queue.get()

    def size(self) -> int:
        """Return the current number of items in the queue."""
        return self._queue.qsize()

    def is_full(self) -> bool:
        """Return True if the queue has reached its max size.

        Always returns False if max_size is 0 (unlimited).
        """
        if self._max_size == 0:
            return False
        return self._queue.full()
