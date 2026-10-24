"""Bounded channel queues with reservations and delayed retry scheduling.

Low-level dequeue removes an item. Workers use claim/ack instead, preserving a
capacity reservation until delivery finishes or the item is rescheduled.
"""

from __future__ import annotations

import asyncio
import datetime
import heapq
import itertools
import time
import uuid
from typing import Optional

from notification_system.exceptions import DuplicateNotificationError, QueueFullError
from notification_system.models import Channel, NotificationTask


class MessageQueue:
    """In-memory queues for one asyncio event loop; restart loses their data."""

    lease_seconds = 30.0

    def __init__(self, max_depth: int = 10000) -> None:
        if type(max_depth) is not int or max_depth <= 0:
            raise ValueError("max_depth must be a positive integer")
        self._max_depth = max_depth
        self._queues = {channel: [] for channel in Channel}
        self._changed = {channel: asyncio.Event() for channel in Channel}
        self._sequence = itertools.count()
        self._claims: dict[str, NotificationTask] = {}
        self._events: dict[tuple[str, Channel, str], str] = {}

    def _insert(self, task: NotificationTask, delay: float = 0.0) -> None:
        heapq.heappush(
            self._queues[task.channel],
            (time.monotonic() + max(0.0, delay), next(self._sequence), task),
        )
        self._changed[task.channel].set()

    async def enqueue(self, task: NotificationTask) -> None:
        event_key = (task.request.recipient_id, task.channel, task.request.event_id)
        if task.request.event_id is not None and event_key in self._events:
            raise DuplicateNotificationError(self._events[event_key])
        if self.is_full(task.channel):
            raise QueueFullError(task.channel.value, self._max_depth)
        delay = 0.0
        if task.scheduled_at is not None:
            delay = task.scheduled_at.timestamp() - time.time()
        self._insert(task, delay)
        if task.request.event_id is not None:
            self._events[event_key] = task.notification_id

    async def dequeue(self, channel: Channel) -> NotificationTask:
        while True:
            task = self.dequeue_nowait(channel)
            if task is not None:
                return task
            changed = self._changed[channel]
            changed.clear()
            pending = self._queues[channel]
            timeout = max(0.001, pending[0][0] - time.monotonic()) if pending else None
            try:
                await asyncio.wait_for(changed.wait(), timeout)
            except asyncio.TimeoutError:
                pass

    def dequeue_nowait(self, channel: Channel) -> Optional[NotificationTask]:
        pending = self._queues[channel]
        if pending and pending[0][0] <= time.monotonic():
            return heapq.heappop(pending)[2]
        return None

    async def claim(self, channel: Channel) -> NotificationTask:
        task = await self.dequeue(channel)
        task.receipt = uuid.uuid4().hex
        self._claims[task.receipt] = task
        return task

    async def ack(self, task: NotificationTask, status=None, details=None) -> None:
        """Release the reservation after terminal delivery."""
        if task.receipt is not None and task.receipt not in self._claims:
            raise RuntimeError("Notification claim was already released")
        self._claims.pop(task.receipt, None)

    async def renew(self, task: NotificationTask) -> bool:
        return task.receipt in self._claims

    async def reschedule(self, task: NotificationTask, delay: float = 0.0) -> None:
        """Transfer an existing reservation to a delayed item without overflow."""
        if task.receipt is not None and task.receipt not in self._claims:
            raise RuntimeError("Notification claim was already released")
        owned = self._claims.pop(task.receipt, None)
        if owned is None and self.is_full(task.channel):
            raise QueueFullError(task.channel.value, self._max_depth)
        task.scheduled_at = datetime.datetime.now(
            datetime.timezone.utc
        ) + datetime.timedelta(seconds=delay)
        self._insert(task, delay)

    async def event_notification_id(self, recipient_id, channel, event_id):
        return self._events.get((recipient_id, channel, event_id))

    def depth(self, channel: Channel) -> int:
        return len(self._queues[channel])

    def in_flight(self, channel: Channel) -> int:
        return sum(task.channel == channel for task in self._claims.values())

    def is_full(self, channel: Channel) -> bool:
        return self.depth(channel) + self.in_flight(channel) >= self._max_depth

    def is_empty(self, channel: Channel) -> bool:
        return self.depth(channel) == 0
