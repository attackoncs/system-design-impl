"""Quick functional test for Worker and WorkerPool."""
import asyncio

from notification_system.worker import Worker, WorkerPool
from notification_system.queue import MessageQueue
from notification_system.provider import SimulatedProvider
from notification_system.tracker import EventTracker
from notification_system.retry import RetryHandler
from notification_system.config import RetryConfig
from notification_system.log import NotificationLog
from notification_system.models import (
    Channel,
    NotificationTask,
    NotificationRequest,
    NotificationStatus,
)


async def test_worker_pool_processes_task():
    """Test that worker pool processes a task and records SENT."""
    q = MessageQueue(100)
    tracker = EventTracker()
    log = NotificationLog()
    rh = RetryHandler(RetryConfig(), q, tracker, log)
    p = SimulatedProvider(Channel.EMAIL, latency_ms=10, failure_rate=0.0)
    providers = {Channel.EMAIL: p}
    pool = WorkerPool(q, providers, tracker, rh, workers_per_channel=2)

    await pool.start()

    req = NotificationRequest(
        recipient_id="user1", channel=Channel.EMAIL, title="Hi", body="Test"
    )
    task = NotificationTask(notification_id="n1", request=req, channel=Channel.EMAIL)

    # Record initial states
    tracker.record("n1", Channel.EMAIL, NotificationStatus.CREATED)
    tracker.record("n1", Channel.EMAIL, NotificationStatus.QUEUED)

    await q.enqueue(task)
    await asyncio.sleep(0.5)
    await pool.stop(graceful=True)

    status = tracker.get_current_status("n1")
    assert status == NotificationStatus.SENT, f"Expected SENT, got {status}"
    print("✓ Worker pool processes task and records SENT")


async def test_worker_pool_graceful_shutdown():
    """Test graceful shutdown waits for in-flight tasks."""
    q = MessageQueue(100)
    tracker = EventTracker()
    log = NotificationLog()
    rh = RetryHandler(RetryConfig(), q, tracker, log)
    p = SimulatedProvider(Channel.SMS, latency_ms=100, failure_rate=0.0)
    providers = {Channel.SMS: p}
    pool = WorkerPool(q, providers, tracker, rh, workers_per_channel=1)

    await pool.start()

    req = NotificationRequest(
        recipient_id="user2", channel=Channel.SMS, title="Hello", body="World"
    )
    task = NotificationTask(notification_id="n2", request=req, channel=Channel.SMS)
    tracker.record("n2", Channel.SMS, NotificationStatus.CREATED)
    tracker.record("n2", Channel.SMS, NotificationStatus.QUEUED)

    await q.enqueue(task)
    await asyncio.sleep(0.05)  # Let worker pick up the task
    await pool.stop(graceful=True)

    status = tracker.get_current_status("n2")
    assert status == NotificationStatus.SENT, f"Expected SENT, got {status}"
    print("✓ Graceful shutdown completes in-flight deliveries")


async def test_worker_delegates_failure_to_retry():
    """Test that failed deliveries are delegated to retry handler."""
    q = MessageQueue(100)
    tracker = EventTracker()
    log = NotificationLog()
    rh = RetryHandler(RetryConfig(max_retries=0), q, tracker, log)
    p = SimulatedProvider(Channel.IOS_PUSH, latency_ms=5, failure_rate=1.0)
    providers = {Channel.IOS_PUSH: p}
    pool = WorkerPool(q, providers, tracker, rh, workers_per_channel=1)

    await pool.start()

    req = NotificationRequest(
        recipient_id="user3", channel=Channel.IOS_PUSH, title="Push", body="Fail"
    )
    task = NotificationTask(notification_id="n3", request=req, channel=Channel.IOS_PUSH)
    tracker.record("n3", Channel.IOS_PUSH, NotificationStatus.CREATED)
    tracker.record("n3", Channel.IOS_PUSH, NotificationStatus.QUEUED)

    await q.enqueue(task)
    await asyncio.sleep(0.5)
    await pool.stop(graceful=True)

    status = tracker.get_current_status("n3")
    assert status == NotificationStatus.FAILED, f"Expected FAILED, got {status}"
    print("✓ Failed delivery delegated to retry handler (marked FAILED)")


async def test_configurable_workers_per_channel():
    """Test that the pool spawns the configured number of workers."""
    q = MessageQueue(100)
    tracker = EventTracker()
    log = NotificationLog()
    rh = RetryHandler(RetryConfig(), q, tracker, log)
    p = SimulatedProvider(Channel.ANDROID_PUSH, latency_ms=5, failure_rate=0.0)
    providers = {Channel.ANDROID_PUSH: p}
    pool = WorkerPool(q, providers, tracker, rh, workers_per_channel=5)

    await pool.start()
    workers = pool.workers
    assert Channel.ANDROID_PUSH in workers
    assert len(workers[Channel.ANDROID_PUSH]) == 5
    await pool.stop(graceful=True)
    print("✓ Configurable workers_per_channel spawns correct count")


async def main():
    await test_worker_pool_processes_task()
    await test_worker_pool_graceful_shutdown()
    await test_worker_delegates_failure_to_retry()
    await test_configurable_workers_per_channel()
    print("\nAll worker pool tests PASSED!")


if __name__ == "__main__":
    asyncio.run(main())
