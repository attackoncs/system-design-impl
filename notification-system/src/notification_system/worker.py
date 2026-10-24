"""Worker pool for async notification delivery.

Implements Worker (single async consumer) and WorkerPool (lifecycle manager)
that consume tasks from per-channel queues, invoke providers, record events,
and delegate failures to the RetryHandler.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
from typing import Optional

import notification_system.exceptions
import notification_system.models
import notification_system.provider
import notification_system.queue
import notification_system.retry
import notification_system.tracker
import notification_system.settings

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
        settings: Optional[notification_system.settings.NotificationSettings] = None,
        delivery_timeout: float = 30.0,
    ) -> None:
        self._worker_id = worker_id
        self._channel = channel
        self._queue = queue
        self._provider = provider
        self._tracker = tracker
        self._retry_handler = retry_handler
        self._settings = settings
        self._delivery_timeout = delivery_timeout
        if (
            type(delivery_timeout) not in (int, float)
            or not math.isfinite(delivery_timeout)
            or delivery_timeout <= 0
        ):
            raise ValueError("delivery_timeout must be finite and positive")
        self._running = False
        self._started = asyncio.Event()
        self._stop_requested = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._claim_task: Optional[asyncio.Task] = None

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
        if self._running:
            raise RuntimeError("Worker is already running")
        self._running = True
        self._stop_requested.clear()
        self._task = asyncio.current_task()
        self._started.set()
        failures = 0
        try:
            while self._running:
                self._claim_task = asyncio.create_task(self._queue.claim(self._channel))
                try:
                    notification_task = await self._claim_task
                except asyncio.CancelledError:
                    # A completed claim may race cancellation of its waiter.
                    # Memory reservations require an explicit return.
                    if self._claim_task.done() and not self._claim_task.cancelled():
                        try:
                            claimed = self._claim_task.result()
                            await self._queue.reschedule(claimed)
                        except Exception:
                            logger.exception(
                                "Unable to return completed claim during cancellation"
                            )
                    if not self._running:
                        break
                    raise
                except Exception:
                    failures += 1
                    logger.exception(
                        "Unable to claim a notification on %s", self._channel.value
                    )
                    try:
                        await asyncio.wait_for(
                            self._stop_requested.wait(),
                            min(2.0, 0.1 * (2 ** min(failures - 1, 5))),
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue
                failures = 0
                await self._process_claim(notification_task)
        finally:
            self._running = False

    async def _renew_lease(self, task):
        while True:
            await asyncio.sleep(self._queue.lease_seconds / 3)
            if not await self._queue.renew(task):
                raise RuntimeError("Notification lease lost during delivery")

    async def _process_claim(self, task):
        heartbeat = asyncio.create_task(self._renew_lease(task))
        delivery = asyncio.create_task(self._process_task(task))
        try:
            done, _ = await asyncio.wait(
                (heartbeat, delivery), return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in done and delivery not in done:
                await heartbeat
            await delivery
        except BaseException as error:
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)
            # Preserve accepted work when a worker is cancelled or dependencies fail.
            try:
                await self._queue.reschedule(task, 0.1)
            except Exception:
                logger.exception(
                    "Unable to release %s; durable leases recover it",
                    task.notification_id,
                )
            if isinstance(error, asyncio.CancelledError):
                raise
            logger.exception("Delivery failure isolated for %s", task.notification_id)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def wait_started(self) -> None:
        """Wait until the worker can receive a stop request without losing
        it.
        """
        await self._started.wait()

    async def stop(self) -> None:
        """Signal the worker to stop after current task completes."""
        self._running = False
        self._stop_requested.set()
        if self._claim_task is not None and not self._claim_task.done():
            self._claim_task.cancel()

    async def _process_task(
        self, task: notification_system.models.NotificationTask
    ) -> None:
        """Process a single notification task through the provider.

        Records a SENDING event, invokes the provider, then records
        SENT on success or delegates to RetryHandler on failure.

        Args:
            task: The notification task to process.
        """
        # Interrupted deliveries return to QUEUED before a new attempt.
        if (
            self._tracker.get_current_status(task.notification_id)
            == notification_system.models.NotificationStatus.SENDING
        ):
            self._tracker.record(
                task.notification_id,
                task.channel,
                notification_system.models.NotificationStatus.QUEUED,
            )
        self._tracker.record(
            task.notification_id,
            task.channel,
            notification_system.models.NotificationStatus.SENDING,
        )

        if self._settings is not None and not self._settings.is_opted_in(
            task.request.recipient_id, task.channel
        ):
            await self._queue.ack(
                task, notification_system.models.NotificationStatus.UNSUBSCRIBED
            )
            self._tracker.record(
                task.notification_id,
                task.channel,
                notification_system.models.NotificationStatus.UNSUBSCRIBED,
            )
            return

        # Each registered device receives the resolved payload. Successful
        # endpoints are retained on normal retry, avoiding partial-fanout repeats.
        pending = [
            endpoint
            for endpoint in task.endpoints
            if endpoint not in task.completed_endpoints
            and endpoint not in task.rejected_endpoints
        ]
        if not task.endpoints:
            pending = [None]  # compatibility for manually constructed tasks
        failures = []
        for endpoint in pending:
            if self._settings is not None and not self._settings.is_opted_in(
                task.request.recipient_id, task.channel
            ):
                await self._queue.ack(
                    task, notification_system.models.NotificationStatus.UNSUBSCRIBED
                )
                self._tracker.record(
                    task.notification_id,
                    task.channel,
                    notification_system.models.NotificationStatus.UNSUBSCRIBED,
                )
                return
            try:
                result = await asyncio.wait_for(
                    self._provider.deliver(
                        dataclasses.replace(task, endpoint=endpoint)
                    ),
                    self._delivery_timeout,
                )
                if not isinstance(result, notification_system.models.DeliveryResult):
                    raise TypeError("Provider must return DeliveryResult")
            except notification_system.exceptions.DeliveryError as error:
                result = notification_system.models.DeliveryResult(
                    False, error=error.reason, retryable=error.retryable
                )
            except Exception as error:
                logger.warning(
                    "Provider raised for %s: %s", task.notification_id, error
                )
                result = notification_system.models.DeliveryResult(
                    False, error=str(error)
                )
            if result.success:
                if endpoint is not None:
                    task.completed_endpoints += (endpoint,)
            else:
                failures.append(result)
                if not result.retryable and endpoint is not None:
                    task.rejected_endpoints += (endpoint,)

        if not failures and not task.rejected_endpoints:
            await self._retry_handler.record_success(task)
            await self._queue.ack(
                task, notification_system.models.NotificationStatus.SENT
            )
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
                reason="; ".join(result.error or "Unknown error" for result in failures)
                or f"Permanent rejection at {len(task.rejected_endpoints)} endpoint(s)",
                retryable=any(result.retryable for result in failures),
            )
            await self._retry_handler.handle_failure(task, error, defer=True)


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
        settings: Optional[notification_system.settings.NotificationSettings] = None,
        delivery_timeout: float = 30.0,
    ) -> None:
        self._queue = queue
        self._providers = providers
        self._tracker = tracker
        self._retry_handler = retry_handler
        self._settings = settings
        self._delivery_timeout = delivery_timeout
        self._lifecycle = asyncio.Lock()
        if type(workers_per_channel) is not int or workers_per_channel <= 0:
            raise ValueError("workers_per_channel must be a positive integer")
        self._workers_per_channel = workers_per_channel
        self._workers: dict[notification_system.models.Channel, list[Worker]] = {}
        self._tasks: list[asyncio.Task] = []

    @property
    def workers(self) -> dict[notification_system.models.Channel, list[Worker]]:
        """Access the current workers by channel."""
        return {channel: list(workers) for channel, workers in self._workers.items()}

    @property
    def is_running(self) -> bool:
        """Whether the worker pool has active tasks."""
        return any(not task.done() for task in self._tasks)

    async def start(self) -> None:
        async with self._lifecycle:
            if self.is_running:
                raise RuntimeError("WorkerPool is already running")
            try:
                await self._start()
            except BaseException:
                await self._stop(graceful=False)
                raise

    async def _start(self) -> None:
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
                    settings=self._settings,
                    delivery_timeout=self._delivery_timeout,
                )
                self._workers[channel].append(worker)
                task = asyncio.create_task(worker.start())
                self._tasks.append(task)
                # Do not let an immediate stop race with start's running flag.
                await worker.wait_started()

    async def stop(self, graceful: bool = True) -> None:
        async with self._lifecycle:
            await self._stop(graceful)

    async def _stop(self, graceful: bool) -> None:
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
