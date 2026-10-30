"""Bounded local fanout queue with claim acknowledgements and delayed retry.

All queue operations belong to one asyncio event loop. Claimed jobs retain
capacity until acknowledged or moved to the bounded dead-letter buffer.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import heapq
import itertools
import math
import time
import uuid

import news_feed_system.models


@dataclasses.dataclass(frozen=True)
class QueueClaim:
    """A queue-owned delivery and its unique acknowledgement receipt."""

    task: news_feed_system.models.FanoutTask
    receipt: str
    attempts: int


@dataclasses.dataclass(frozen=True)
class FailedTask:
    """A terminal failure retained for bounded local inspection/replay."""

    task: news_feed_system.models.FanoutTask
    attempts: int
    error: str


class MessageQueue:
    """Ready FIFO jobs, bounded including in-flight and delayed retry jobs.

    ``max_size=0`` explicitly permits unbounded buffering. The queue and dead
    letters are process-local; retain accepted publications in a durable
    post store/outbox when restart recovery is required.
    """

    def __init__(
        self, max_size: int = 10000, *, dead_letter_capacity: int = 100
    ) -> None:
        for name, value in (
            ("max_size", max_size),
            ("dead_letter_capacity", dead_letter_capacity),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if dead_letter_capacity == 0:
            raise ValueError("dead_letter_capacity must be positive")
        self._max_size = max_size
        self._pending: list[
            tuple[float, int, news_feed_system.models.FanoutTask, int]
        ] = []
        self._sequence = itertools.count()
        self._inflight: dict[str, QueueClaim] = {}
        self._changed = asyncio.Event()
        self._drained = asyncio.Event()
        self._drained.set()
        self._dead_letters: collections.deque[FailedTask] = collections.deque(
            maxlen=dead_letter_capacity
        )
        self._failed_count = 0

    async def enqueue(self, task: news_feed_system.models.FanoutTask) -> None:
        """Wait for a reservation and enqueue a snapshot of the task."""
        snapshot = dataclasses.replace(task, target_user_ids=list(task.target_user_ids))
        while self.is_full():
            self._changed.clear()
            await self._changed.wait()
        self._push(snapshot, attempts=0, due=time.monotonic())
        self._drained.clear()

    def _push(
        self, task: news_feed_system.models.FanoutTask, attempts: int, due: float
    ) -> None:
        heapq.heappush(self._pending, (due, next(self._sequence), task, attempts))
        self._changed.set()

    async def claim(self) -> QueueClaim:
        """Claim a ready task without releasing its capacity reservation."""
        while True:
            if self._pending:
                due, _, task, attempts = self._pending[0]
                delay = due - time.monotonic()
                if delay <= 0:
                    heapq.heappop(self._pending)
                    claim = QueueClaim(task, uuid.uuid4().hex, attempts + 1)
                    self._inflight[claim.receipt] = claim
                    return claim
            else:
                delay = None
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def dequeue(self) -> news_feed_system.models.FanoutTask:
        """Destructively dequeue for manual consumers; workers use claims."""
        claim = await self.claim()
        self.ack(claim)
        return claim.task

    def _owned(self, claim: QueueClaim) -> QueueClaim:
        current = self._inflight.get(claim.receipt)
        if current is not claim:
            raise ValueError("stale or foreign queue claim")
        return current

    def ack(self, claim: QueueClaim) -> None:
        """Acknowledge exactly one currently owned delivery."""
        self._owned(claim)
        del self._inflight[claim.receipt]
        self._released()

    def retry(self, claim: QueueClaim, delay_seconds: float = 0.0) -> None:
        """Return a claim using its existing reserved capacity slot."""
        if (
            not isinstance(delay_seconds, (int, float))
            or not math.isfinite(delay_seconds)
            or delay_seconds < 0
        ):
            raise ValueError("delay_seconds must be finite and non-negative")
        self._owned(claim)
        del self._inflight[claim.receipt]
        self._push(claim.task, claim.attempts, time.monotonic() + delay_seconds)

    def fail(self, claim: QueueClaim, error: str) -> None:
        """Release a failed job and retain a bounded failure record."""
        self._owned(claim)
        self._dead_letters.append(FailedTask(claim.task, claim.attempts, str(error)))
        self._failed_count += 1
        del self._inflight[claim.receipt]
        self._released()

    def _released(self) -> None:
        self._changed.set()
        if self.outstanding() == 0:
            self._drained.set()

    async def join(self) -> None:
        """Wait until all accepted work is acknowledged or failed."""
        await self._drained.wait()

    def size(self) -> int:
        """Pending jobs, including retries waiting for their due time."""
        return len(self._pending)

    def in_flight(self) -> int:
        return len(self._inflight)

    def outstanding(self) -> int:
        return self.size() + self.in_flight()

    def is_full(self) -> bool:
        return bool(self._max_size and self.outstanding() >= self._max_size)

    @property
    def dead_letters(self) -> tuple[FailedTask, ...]:
        return tuple(self._dead_letters)

    @property
    def failed_count(self) -> int:
        """Cumulative failures, including records evicted from the buffer."""
        return self._failed_count
