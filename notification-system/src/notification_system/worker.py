"""Worker pool for async notification delivery.

Implements Worker (single async consumer) and WorkerPool (lifecycle manager)
that consume tasks from per-channel queues, invoke providers, record events,
and delegate failures to the RetryHandler.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import notification_system.exceptions
import notification_system.models
import notification_system.provider
import notification_system.queue
import notification_system.retry
import notification_system.tracker

logger = logging.getLogger(__name__)


class Worker:
    """A single async worker that consumes tasks from a channel queue.

    Pulls tasks, invokes the provider, records events, and handles failures by
    delegating to the RetryHandler.
    """

    def __init__(
        self,
        worker_id: str,
        channel: notification_system.models.Channel,
        queue: notification_system.queue.MessageQueue,
        provider: notification_system.provider.Provider,
        tracker: notification_system.tracker.EventTracker,
        retry_handler: notification_system.retry.RetryHandler,
    ) -> None:
        self._worker_id = worker_id
        self._channel = channel
        self._queue = queue
        self._provider = provider
        self._tracker = tracker
        self._retry_handler = retry_handler
        self._running = False
        self._started = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    @property
    def worker_id(self) -> str:
        """The unique identifier for this worker."""
        return self._worker_id

    @property
    def channel(self) -> notification_system.models.Channel:
        """The channel this worker processes."""
        return self._channel

    @property
    def is_running(self) -> bool:
        """Whether the worker loop is currently active."""
        return self._running

    async def start(self) -> None:
        """Start the worker loop.

        Continuously dequeues tasks from the channel queue and processes them
        until stop() is called. Uses a 1-second timeout on dequeue to allow
        periodic checking of the running flag.
        """
        self._running = True
        self._task = asyncio.current_task()
        self._started.set()
        while self._running:
            try:
                notification_task = await asyncio.wait_for(
                    self._queue.dequeue(self._channel), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            await self._process_task(notification_task)

    async def wait_started(self) -> None:
        """Wait until the worker can receive a stop request without losing
        it.
        """
        await self._started.wait()

    async def stop(self) -> None:
        """Signal the worker to stop after current task completes."""
        self._running = False

    async def _process_task(
        self, task: notification_system.models.NotificationTask
    ) -> None:
        """Process a single notification task through the provider.

        Records a SENDING event, invokes the provider, then records
        SENT on success or delegates to RetryHandler on failure.

        Args:
            task: The notification task to process.
        """
        # Record SENDING event
        self._tracker.record(
            task.notification_id,
            task.channel,
            notification_system.models.NotificationStatus.SENDING,
        )

        # Invoke the provider
        result = await self._provider.deliver(task)

        if result.success:
            # Record SENT event on successful delivery
            self._tracker.record(
                task.notification_id,
                task.channel,
                notification_system.models.NotificationStatus.SENT,
            )
        else:
            # Delegate failure to retry handler
            error = notification_system.exceptions.DeliveryError(
                notification_id=task.notification_id,
                channel=task.channel.value,
                reason=result.error or "Unknown error",
                retryable=result.retryable,
            )
            await self._retry_handler.handle_failure(task, error)


class WorkerPool:
    """Pool of async workers per notification channel.

    Manages lifecycle of worker tasks: start, stop, graceful shutdown.
    Spawns a configurable number of workers per channel.
    """

    def __init__(
        self,
        queue: notification_system.queue.MessageQueue,
        providers: dict[
            notification_system.models.Channel,
            notification_system.provider.Provider,
        ],
        tracker: notification_system.tracker.EventTracker,
        retry_handler: notification_system.retry.RetryHandler,
        workers_per_channel: int = 3,
    ) -> None:
        self._queue = queue
        self._providers = providers
        self._tracker = tracker
        self._retry_handler = retry_handler
        if type(workers_per_channel) is not int or workers_per_channel <= 0:
            raise ValueError("workers_per_channel must be a positive integer")
        self._workers_per_channel = workers_per_channel
        self._workers: dict[notification_system.models.Channel, list[Worker]] = {}
        self._tasks: list[asyncio.Task] = []

    @property
    def workers(self) -> dict[notification_system.models.Channel, list[Worker]]:
        """Access the current workers by channel."""
        return dict(self._workers)

    @property
    def is_running(self) -> bool:
        """Whether the worker pool has active tasks."""
        return len(self._tasks) > 0

    async def start(self) -> None:
        """Start all worker tasks for all channels.

        Spawns workers_per_channel asyncio tasks for each channel that has a
        registered provider. Channels without providers are skipped.
        """
        if self.is_running:
            raise RuntimeError("WorkerPool is already running")

        for channel in notification_system.models.Channel:
            provider = self._providers.get(channel)
            if provider is None:
                continue
            self._workers[channel] = []
            for i in range(self._workers_per_channel):
                worker = Worker(
                    worker_id=f"{channel.value}_worker_{i}",
                    channel=channel,
                    queue=self._queue,
                    provider=provider,
                    tracker=self._tracker,
                    retry_handler=self._retry_handler,
                )
                self._workers[channel].append(worker)
                task = asyncio.create_task(worker.start())
                self._tasks.append(task)
                # Do not let an immediate stop race with start's running flag.
                await worker.wait_started()

    async def stop(self, graceful: bool = True) -> None:
        """Stop all workers.

        Args:
            graceful: If True, wait for in-flight deliveries to complete.
                      If False, cancel all worker tasks immediately.
        """
        # Signal all workers to stop
        for channel_workers in self._workers.values():
            for worker in channel_workers:
                await worker.stop()

        if graceful:
            # Wait for all tasks to finish naturally
            await asyncio.gather(*self._tasks, return_exceptions=True)
        else:
            # Cancel all tasks immediately
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

        self._tasks.clear()
        self._workers.clear()
