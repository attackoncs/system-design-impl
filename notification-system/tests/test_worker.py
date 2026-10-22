"""Unit tests for notification_system.worker module (Worker and WorkerPool).

Tests Worker: processes task from queue, records SENDING then SENT events on
success, delegates to retry handler on failure.

Tests WorkerPool: starts workers for channels with providers, graceful
shutdown.

Uses asyncio and SimulatedProvider with 0 latency for fast tests.
"""

import asyncio

import notification_system.config
import notification_system.log
import notification_system.models
import notification_system.provider
import notification_system.queue
import notification_system.retry
import notification_system.tracker
import notification_system.worker

# --- Helpers ---


def _make_task(
    notification_id: str = "n1",
    channel: notification_system.models.Channel = notification_system.models.Channel.EMAIL,
    recipient_id: str = "user1",
) -> notification_system.models.NotificationTask:
    """Create a NotificationTask for testing."""
    req = notification_system.models.NotificationRequest(
        recipient_id=recipient_id, channel=channel, title="Test", body="Hello"
    )
    return notification_system.models.NotificationTask(
        notification_id=notification_id, request=req, channel=channel
    )


def _make_dependencies(
    channel: notification_system.models.Channel = notification_system.models.Channel.EMAIL,
    latency_ms: float = 0.0,
    failure_rate: float = 0.0,
    max_retries: int = 3,
):
    """Create shared test dependencies."""
    queue = notification_system.queue.MessageQueue(max_depth=100)
    tracker = notification_system.tracker.EventTracker()
    log = notification_system.log.NotificationLog()
    retry_handler = notification_system.retry.RetryHandler(
        notification_system.config.RetryConfig(max_retries=max_retries, base_delay=0.01, max_delay=0.05),
        queue,
        tracker,
        log,
    )
    provider = notification_system.provider.SimulatedProvider(
        channel, latency_ms=latency_ms, failure_rate=failure_rate
    )
    return queue, tracker, retry_handler, provider


# --- Worker Tests ---


class TestWorker:
    """Tests for the Worker class."""

    async def test_worker_processes_task_from_queue(self):
        """Worker dequeues and processes a task from the channel queue."""
        queue, tracker, retry_handler, provider = _make_dependencies(
            notification_system.models.Channel.EMAIL, latency_ms=0.0, failure_rate=0.0
        )
        worker = notification_system.worker.Worker(
            worker_id="email_worker_0",
            channel=notification_system.models.Channel.EMAIL,
            queue=queue,
            provider=provider,
            tracker=tracker,
            retry_handler=retry_handler,
        )

        task = _make_task("n1", notification_system.models.Channel.EMAIL)
        # Set up valid state transitions before worker processes
        tracker.record("n1", notification_system.models.Channel.EMAIL, notification_system.models.NotificationStatus.CREATED)
        tracker.record("n1", notification_system.models.Channel.EMAIL, notification_system.models.NotificationStatus.QUEUED)

        await queue.enqueue(task)

        # Start worker in background, let it process, then stop
        worker_task = asyncio.create_task(worker.start())
        await asyncio.sleep(0.05)
        await worker.stop()
        await asyncio.wait_for(worker_task, timeout=2.0)

        # Queue should be empty after processing
        assert queue.is_empty(notification_system.models.Channel.EMAIL)

    async def test_worker_records_sending_then_sent_on_success(self):
        """Worker records SENDING then SENT events on successful delivery."""
        queue, tracker, retry_handler, provider = _make_dependencies(
            notification_system.models.Channel.EMAIL, latency_ms=0.0, failure_rate=0.0
        )
        worker = notification_system.worker.Worker(
            worker_id="email_worker_0",
            channel=notification_system.models.Channel.EMAIL,
            queue=queue,
            provider=provider,
            tracker=tracker,
            retry_handler=retry_handler,
        )

        task = _make_task("n1", notification_system.models.Channel.EMAIL)
        tracker.record("n1", notification_system.models.Channel.EMAIL, notification_system.models.NotificationStatus.CREATED)
        tracker.record("n1", notification_system.models.Channel.EMAIL, notification_system.models.NotificationStatus.QUEUED)

        await queue.enqueue(task)

        worker_task = asyncio.create_task(worker.start())
        await asyncio.sleep(0.05)
        await worker.stop()
        await asyncio.wait_for(worker_task, timeout=2.0)

        history = tracker.get_history("n1")
        statuses = [e.status for e in history]
        assert notification_system.models.NotificationStatus.SENDING in statuses
        assert notification_system.models.NotificationStatus.SENT in statuses
        # SENDING must come before SENT
        sending_idx = statuses.index(notification_system.models.NotificationStatus.SENDING)
        sent_idx = statuses.index(notification_system.models.NotificationStatus.SENT)
        assert sending_idx < sent_idx

    async def test_worker_delegates_failure_to_retry_handler(self):
        """Worker delegates to retry handler on delivery failure."""
        queue, tracker, retry_handler, provider = _make_dependencies(
            notification_system.models.Channel.SMS, latency_ms=0.0, failure_rate=1.0, max_retries=0
        )
        worker = notification_system.worker.Worker(
            worker_id="sms_worker_0",
            channel=notification_system.models.Channel.SMS,
            queue=queue,
            provider=provider,
            tracker=tracker,
            retry_handler=retry_handler,
        )

        task = _make_task("n2", notification_system.models.Channel.SMS)
        tracker.record("n2", notification_system.models.Channel.SMS, notification_system.models.NotificationStatus.CREATED)
        tracker.record("n2", notification_system.models.Channel.SMS, notification_system.models.NotificationStatus.QUEUED)

        await queue.enqueue(task)

        worker_task = asyncio.create_task(worker.start())
        await asyncio.sleep(0.1)
        await worker.stop()
        await asyncio.wait_for(worker_task, timeout=2.0)

        # With max_retries=0, the retry handler marks it FAILED
        status = tracker.get_current_status("n2")
        assert status == notification_system.models.NotificationStatus.FAILED

    async def test_worker_id_and_channel_properties(self):
        """Worker exposes worker_id and channel properties."""
        queue, tracker, retry_handler, provider = _make_dependencies(
            notification_system.models.Channel.IOS_PUSH
        )
        worker = notification_system.worker.Worker(
            worker_id="ios_push_worker_2",
            channel=notification_system.models.Channel.IOS_PUSH,
            queue=queue,
            provider=provider,
            tracker=tracker,
            retry_handler=retry_handler,
        )
        assert worker.worker_id == "ios_push_worker_2"
        assert worker.channel == notification_system.models.Channel.IOS_PUSH

    async def test_worker_is_running_property(self):
        """Worker.is_running reflects the running state."""
        queue, tracker, retry_handler, provider = _make_dependencies(
            notification_system.models.Channel.EMAIL
        )
        worker = notification_system.worker.Worker(
            worker_id="email_worker_0",
            channel=notification_system.models.Channel.EMAIL,
            queue=queue,
            provider=provider,
            tracker=tracker,
            retry_handler=retry_handler,
        )
        assert worker.is_running is False

        worker_task = asyncio.create_task(worker.start())
        await asyncio.sleep(0.05)
        assert worker.is_running is True

        await worker.stop()
        await asyncio.wait_for(worker_task, timeout=2.0)
        assert worker.is_running is False


# --- WorkerPool Tests ---


class TestWorkerPool:
    """Tests for the WorkerPool class."""

    async def test_pool_starts_workers_for_channels_with_providers(self):
        """WorkerPool spawns workers only for channels that have providers."""
        queue, tracker, retry_handler, _ = _make_dependencies(notification_system.models.Channel.EMAIL)
        email_provider = notification_system.provider.SimulatedProvider(notification_system.models.Channel.EMAIL, latency_ms=0.0)
        sms_provider = notification_system.provider.SimulatedProvider(notification_system.models.Channel.SMS, latency_ms=0.0)
        providers = {notification_system.models.Channel.EMAIL: email_provider, notification_system.models.Channel.SMS: sms_provider}

        pool = notification_system.worker.WorkerPool(
            queue=queue,
            providers=providers,
            tracker=tracker,
            retry_handler=retry_handler,
            workers_per_channel=2,
        )

        await pool.start()

        workers = pool.workers
        # Only EMAIL and SMS should have workers (not IOS_PUSH or ANDROID_PUSH)
        assert notification_system.models.Channel.EMAIL in workers
        assert notification_system.models.Channel.SMS in workers
        assert notification_system.models.Channel.IOS_PUSH not in workers
        assert notification_system.models.Channel.ANDROID_PUSH not in workers
        assert len(workers[notification_system.models.Channel.EMAIL]) == 2
        assert len(workers[notification_system.models.Channel.SMS]) == 2

        await pool.stop(graceful=True)

    async def test_pool_skips_channels_without_providers(self):
        """WorkerPool does not spawn workers for channels without providers."""
        queue, tracker, retry_handler, _ = _make_dependencies(notification_system.models.Channel.EMAIL)
        providers = {
            notification_system.models.Channel.EMAIL: notification_system.provider.SimulatedProvider(notification_system.models.Channel.EMAIL, latency_ms=0.0)
        }

        pool = notification_system.worker.WorkerPool(
            queue=queue,
            providers=providers,
            tracker=tracker,
            retry_handler=retry_handler,
            workers_per_channel=3,
        )

        await pool.start()

        workers = pool.workers
        assert len(workers) == 1
        assert notification_system.models.Channel.EMAIL in workers
        assert len(workers[notification_system.models.Channel.EMAIL]) == 3

        await pool.stop(graceful=True)

    async def test_pool_graceful_shutdown_completes_inflight(self):
        """Graceful shutdown waits for in-flight deliveries to complete."""
        queue, tracker, retry_handler, _ = _make_dependencies(notification_system.models.Channel.EMAIL)
        # Use a small latency to simulate in-flight delivery
        provider = notification_system.provider.SimulatedProvider(
            notification_system.models.Channel.EMAIL, latency_ms=50.0, failure_rate=0.0
        )
        providers = {notification_system.models.Channel.EMAIL: provider}

        pool = notification_system.worker.WorkerPool(
            queue=queue,
            providers=providers,
            tracker=tracker,
            retry_handler=retry_handler,
            workers_per_channel=1,
        )

        await pool.start()

        task = _make_task("n_inflight", notification_system.models.Channel.EMAIL)
        tracker.record("n_inflight", notification_system.models.Channel.EMAIL, notification_system.models.NotificationStatus.CREATED)
        tracker.record("n_inflight", notification_system.models.Channel.EMAIL, notification_system.models.NotificationStatus.QUEUED)
        await queue.enqueue(task)

        # Give worker time to pick up the task
        await asyncio.sleep(0.02)
        # Graceful stop should wait for delivery to finish
        await pool.stop(graceful=True)

        status = tracker.get_current_status("n_inflight")
        assert status == notification_system.models.NotificationStatus.SENT

    async def test_pool_is_running_property(self):
        """WorkerPool.is_running reflects active task state."""
        queue, tracker, retry_handler, _ = _make_dependencies(notification_system.models.Channel.EMAIL)
        provider = notification_system.provider.SimulatedProvider(notification_system.models.Channel.EMAIL, latency_ms=0.0)
        providers = {notification_system.models.Channel.EMAIL: provider}

        pool = notification_system.worker.WorkerPool(
            queue=queue,
            providers=providers,
            tracker=tracker,
            retry_handler=retry_handler,
            workers_per_channel=1,
        )

        assert pool.is_running is False
        await pool.start()
        assert pool.is_running is True
        await pool.stop(graceful=True)
        assert pool.is_running is False

    async def test_pool_workers_per_channel_configurable(self):
        """WorkerPool respects workers_per_channel configuration."""
        queue, tracker, retry_handler, _ = _make_dependencies(
            notification_system.models.Channel.ANDROID_PUSH
        )
        provider = notification_system.provider.SimulatedProvider(notification_system.models.Channel.ANDROID_PUSH, latency_ms=0.0)
        providers = {notification_system.models.Channel.ANDROID_PUSH: provider}

        pool = notification_system.worker.WorkerPool(
            queue=queue,
            providers=providers,
            tracker=tracker,
            retry_handler=retry_handler,
            workers_per_channel=5,
        )

        await pool.start()
        workers = pool.workers
        assert len(workers[notification_system.models.Channel.ANDROID_PUSH]) == 5
        await pool.stop(graceful=True)

    async def test_pool_processes_task_end_to_end(self):
        """WorkerPool processes a task through the full pipeline."""
        queue, tracker, retry_handler, _ = _make_dependencies(notification_system.models.Channel.EMAIL)
        provider = notification_system.provider.SimulatedProvider(
            notification_system.models.Channel.EMAIL, latency_ms=0.0, failure_rate=0.0
        )
        providers = {notification_system.models.Channel.EMAIL: provider}

        pool = notification_system.worker.WorkerPool(
            queue=queue,
            providers=providers,
            tracker=tracker,
            retry_handler=retry_handler,
            workers_per_channel=2,
        )

        await pool.start()

        task = _make_task("n_e2e", notification_system.models.Channel.EMAIL)
        tracker.record("n_e2e", notification_system.models.Channel.EMAIL, notification_system.models.NotificationStatus.CREATED)
        tracker.record("n_e2e", notification_system.models.Channel.EMAIL, notification_system.models.NotificationStatus.QUEUED)
        await queue.enqueue(task)

        await asyncio.sleep(0.05)
        await pool.stop(graceful=True)

        status = tracker.get_current_status("n_e2e")
        assert status == notification_system.models.NotificationStatus.SENT

    async def test_pool_non_graceful_shutdown_cancels_tasks(self):
        """Non-graceful shutdown cancels worker tasks immediately."""
        queue, tracker, retry_handler, _ = _make_dependencies(notification_system.models.Channel.EMAIL)
        provider = notification_system.provider.SimulatedProvider(notification_system.models.Channel.EMAIL, latency_ms=0.0)
        providers = {notification_system.models.Channel.EMAIL: provider}

        pool = notification_system.worker.WorkerPool(
            queue=queue,
            providers=providers,
            tracker=tracker,
            retry_handler=retry_handler,
            workers_per_channel=2,
        )

        await pool.start()
        assert pool.is_running is True

        await pool.stop(graceful=False)
        assert pool.is_running is False


async def test_pool_immediate_graceful_stop_does_not_lose_stop_signal():
    """A pool can stop before its first delivery without restarting workers."""
    queue, tracker, retry_handler, provider = _make_dependencies(notification_system.models.Channel.EMAIL)
    pool = notification_system.worker.WorkerPool(
        queue=queue,
        providers={notification_system.models.Channel.EMAIL: provider},
        tracker=tracker,
        retry_handler=retry_handler,
        workers_per_channel=2,
    )
    await pool.start()
    await asyncio.wait_for(pool.stop(graceful=True), timeout=3)
    assert not pool.is_running
