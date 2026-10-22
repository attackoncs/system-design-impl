"""Quick functional test for Worker and WorkerPool."""

import asyncio

import notification_system.config
import notification_system.log
import notification_system.models
import notification_system.provider
import notification_system.queue
import notification_system.retry
import notification_system.tracker
import notification_system.worker


async def test_worker_pool_processes_task():
    """Test that worker pool processes a task and records SENT."""
    q = notification_system.queue.MessageQueue(100)
    tracker = notification_system.tracker.EventTracker()
    log = notification_system.log.NotificationLog()
    rh = notification_system.retry.RetryHandler(notification_system.config.RetryConfig(), q, tracker, log)
    p = notification_system.provider.SimulatedProvider(notification_system.models.Channel.EMAIL, latency_ms=10, failure_rate=0.0)
    providers = {notification_system.models.Channel.EMAIL: p}
    pool = notification_system.worker.WorkerPool(q, providers, tracker, rh, workers_per_channel=2)

    await pool.start()

    req = notification_system.models.NotificationRequest(
        recipient_id="user1", channel=notification_system.models.Channel.EMAIL, title="Hi", body="Test"
    )
    task = notification_system.models.NotificationTask(
        notification_id="n1", request=req, channel=notification_system.models.Channel.EMAIL
    )

    # Record initial states
    tracker.record("n1", notification_system.models.Channel.EMAIL, notification_system.models.NotificationStatus.CREATED)
    tracker.record("n1", notification_system.models.Channel.EMAIL, notification_system.models.NotificationStatus.QUEUED)

    await q.enqueue(task)
    await asyncio.sleep(0.5)
    await pool.stop(graceful=True)

    status = tracker.get_current_status("n1")
    assert status == notification_system.models.NotificationStatus.SENT, f"Expected SENT, got {status}"
    print("✓ Worker pool processes task and records SENT")


async def test_worker_pool_graceful_shutdown():
    """Test graceful shutdown waits for in-flight tasks."""
    q = notification_system.queue.MessageQueue(100)
    tracker = notification_system.tracker.EventTracker()
    log = notification_system.log.NotificationLog()
    rh = notification_system.retry.RetryHandler(notification_system.config.RetryConfig(), q, tracker, log)
    p = notification_system.provider.SimulatedProvider(notification_system.models.Channel.SMS, latency_ms=100, failure_rate=0.0)
    providers = {notification_system.models.Channel.SMS: p}
    pool = notification_system.worker.WorkerPool(q, providers, tracker, rh, workers_per_channel=1)

    await pool.start()

    req = notification_system.models.NotificationRequest(
        recipient_id="user2", channel=notification_system.models.Channel.SMS, title="Hello", body="World"
    )
    task = notification_system.models.NotificationTask(
        notification_id="n2", request=req, channel=notification_system.models.Channel.SMS
    )
    tracker.record("n2", notification_system.models.Channel.SMS, notification_system.models.NotificationStatus.CREATED)
    tracker.record("n2", notification_system.models.Channel.SMS, notification_system.models.NotificationStatus.QUEUED)

    await q.enqueue(task)
    await asyncio.sleep(0.05)  # Let worker pick up the task
    await pool.stop(graceful=True)

    status = tracker.get_current_status("n2")
    assert status == notification_system.models.NotificationStatus.SENT, f"Expected SENT, got {status}"
    print("✓ Graceful shutdown completes in-flight deliveries")


async def test_worker_delegates_failure_to_retry():
    """Test that failed deliveries are delegated to retry handler."""
    q = notification_system.queue.MessageQueue(100)
    tracker = notification_system.tracker.EventTracker()
    log = notification_system.log.NotificationLog()
    rh = notification_system.retry.RetryHandler(notification_system.config.RetryConfig(max_retries=0), q, tracker, log)
    p = notification_system.provider.SimulatedProvider(notification_system.models.Channel.IOS_PUSH, latency_ms=5, failure_rate=1.0)
    providers = {notification_system.models.Channel.IOS_PUSH: p}
    pool = notification_system.worker.WorkerPool(q, providers, tracker, rh, workers_per_channel=1)

    await pool.start()

    req = notification_system.models.NotificationRequest(
        recipient_id="user3",
        channel=notification_system.models.Channel.IOS_PUSH,
        title="Push",
        body="Fail",
    )
    task = notification_system.models.NotificationTask(
        notification_id="n3", request=req, channel=notification_system.models.Channel.IOS_PUSH
    )
    tracker.record("n3", notification_system.models.Channel.IOS_PUSH, notification_system.models.NotificationStatus.CREATED)
    tracker.record("n3", notification_system.models.Channel.IOS_PUSH, notification_system.models.NotificationStatus.QUEUED)

    await q.enqueue(task)
    await asyncio.sleep(0.5)
    await pool.stop(graceful=True)

    status = tracker.get_current_status("n3")
    assert status == notification_system.models.NotificationStatus.FAILED, f"Expected FAILED, got {status}"
    print("✓ Failed delivery delegated to retry handler (marked FAILED)")


async def test_configurable_workers_per_channel():
    """Test that the pool spawns the configured number of workers."""
    q = notification_system.queue.MessageQueue(100)
    tracker = notification_system.tracker.EventTracker()
    log = notification_system.log.NotificationLog()
    rh = notification_system.retry.RetryHandler(notification_system.config.RetryConfig(), q, tracker, log)
    p = notification_system.provider.SimulatedProvider(notification_system.models.Channel.ANDROID_PUSH, latency_ms=5, failure_rate=0.0)
    providers = {notification_system.models.Channel.ANDROID_PUSH: p}
    pool = notification_system.worker.WorkerPool(q, providers, tracker, rh, workers_per_channel=5)

    await pool.start()
    workers = pool.workers
    assert notification_system.models.Channel.ANDROID_PUSH in workers
    assert len(workers[notification_system.models.Channel.ANDROID_PUSH]) == 5
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
