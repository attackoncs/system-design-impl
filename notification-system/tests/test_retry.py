"""Unit tests for RetryHandler.

Tests compute_delay (exponential backoff, max_delay cap, jitter non-negative),
handle_failure with retryable error (re-enqueues with incremented attempt),
handle_failure with non-retryable error (marks FAILED immediately),
handle_failure when max retries exceeded (marks FAILED, no re-enqueue).
"""

import asyncio
from unittest.mock import patch

import pytest

from notification_system.config import RetryConfig
from notification_system.exceptions import DeliveryError
from notification_system.log import NotificationLog
from notification_system.models import (
    Channel,
    NotificationRequest,
    NotificationStatus,
    NotificationTask,
)
from notification_system.queue import MessageQueue
from notification_system.retry import RetryHandler
from notification_system.tracker import EventTracker


async def _noop_sleep(*args, **kwargs):
    """No-op coroutine to replace asyncio.sleep in tests."""
    return None


@pytest.fixture
def retry_config() -> RetryConfig:
    """Standard retry config for tests."""
    return RetryConfig(
        max_retries=3,
        base_delay=1.0,
        max_delay=60.0,
        jitter_factor=0.1,
    )


@pytest.fixture
def queue() -> MessageQueue:
    return MessageQueue(max_depth=100)


@pytest.fixture
def tracker() -> EventTracker:
    return EventTracker()


@pytest.fixture
def notification_log() -> NotificationLog:
    return NotificationLog()


@pytest.fixture
def handler(retry_config, queue, tracker, notification_log) -> RetryHandler:
    return RetryHandler(
        config=retry_config,
        queue=queue,
        tracker=tracker,
        notification_log=notification_log,
    )


@pytest.fixture
def sample_task() -> NotificationTask:
    """A sample notification task at attempt 0."""
    return NotificationTask(
        notification_id="notif-001",
        request=NotificationRequest(
            recipient_id="user-1",
            channel=Channel.EMAIL,
            title="Test",
            body="Hello world",
        ),
        channel=Channel.EMAIL,
        attempt=0,
    )


class TestComputeDelay:
    """Tests for RetryHandler.compute_delay."""

    def test_exponential_backoff_attempt_zero(self, handler):
        """Attempt 0: base_delay * 2^0 = 1.0 (plus jitter)."""
        with patch("notification_system.retry.random.uniform", return_value=0.0):
            delay = handler.compute_delay(0)
        assert delay == 1.0

    def test_exponential_backoff_attempt_one(self, handler):
        """Attempt 1: base_delay * 2^1 = 2.0 (plus jitter)."""
        with patch("notification_system.retry.random.uniform", return_value=0.0):
            delay = handler.compute_delay(1)
        assert delay == 2.0

    def test_exponential_backoff_attempt_two(self, handler):
        """Attempt 2: base_delay * 2^2 = 4.0 (plus jitter)."""
        with patch("notification_system.retry.random.uniform", return_value=0.0):
            delay = handler.compute_delay(2)
        assert delay == 4.0

    def test_max_delay_cap(self, handler):
        """Delay should never exceed max_delay regardless of attempt."""
        with patch("notification_system.retry.random.uniform", return_value=0.0):
            delay = handler.compute_delay(10)  # 2^10 = 1024 > max_delay of 60
        assert delay == 60.0

    def test_jitter_non_negative(self, handler):
        """Jitter should always add a non-negative value to the delay."""
        # Run multiple times to check jitter is always non-negative
        for attempt in range(5):
            delay = handler.compute_delay(attempt)
            # Without jitter, base delay is base_delay * 2^attempt
            base = handler._config.base_delay * (2**attempt)
            # Delay should be >= base (jitter adds, never subtracts)
            assert delay >= base or delay == handler._config.max_delay

    def test_jitter_bounded_by_factor(self, handler):
        """Jitter should be at most jitter_factor * exponential component."""
        # With max jitter, delay = exponential + jitter_factor * exponential
        exponential = 1.0  # attempt 0
        max_jitter = handler._config.jitter_factor * exponential
        with patch(
            "notification_system.retry.random.uniform", return_value=max_jitter
        ):
            delay = handler.compute_delay(0)
        assert delay == exponential + max_jitter

    def test_delay_with_custom_config(self):
        """Test with different base_delay and max_delay."""
        config = RetryConfig(
            max_retries=5,
            base_delay=2.0,
            max_delay=20.0,
            jitter_factor=0.0,
        )
        h = RetryHandler(
            config=config,
            queue=MessageQueue(),
            tracker=EventTracker(),
            notification_log=NotificationLog(),
        )
        with patch("notification_system.retry.random.uniform", return_value=0.0):
            assert h.compute_delay(0) == 2.0   # 2 * 2^0 = 2
            assert h.compute_delay(1) == 4.0   # 2 * 2^1 = 4
            assert h.compute_delay(2) == 8.0   # 2 * 2^2 = 8
            assert h.compute_delay(3) == 16.0  # 2 * 2^3 = 16
            assert h.compute_delay(4) == 20.0  # 2 * 2^4 = 32, capped at 20


class TestHandleFailureRetryable:
    """Tests for handle_failure with retryable errors."""

    async def test_retryable_error_reenqueues_task(self, handler, queue, sample_task):
        """A retryable error should re-enqueue the task with incremented attempt."""
        # Set up initial state in tracker so FAILED transition is valid
        handler._tracker.record(
            sample_task.notification_id, sample_task.channel, NotificationStatus.CREATED
        )
        handler._tracker.record(
            sample_task.notification_id, sample_task.channel, NotificationStatus.QUEUED
        )
        handler._tracker.record(
            sample_task.notification_id, sample_task.channel, NotificationStatus.SENDING
        )

        error = DeliveryError(
            notification_id="notif-001",
            channel="email",
            reason="Connection timeout",
            retryable=True,
        )

        with patch("notification_system.retry.asyncio.sleep", side_effect=_noop_sleep):
            await handler.handle_failure(sample_task, error)

        # Task should be re-enqueued
        assert not queue.is_empty(Channel.EMAIL)
        retry_task = queue.dequeue_nowait(Channel.EMAIL)
        assert retry_task is not None
        assert retry_task.attempt == 1
        assert retry_task.notification_id == "notif-001"

    async def test_retryable_error_increments_attempt(self, handler, queue):
        """Each retry should increment the attempt counter."""
        task = NotificationTask(
            notification_id="notif-002",
            request=NotificationRequest(
                recipient_id="user-2",
                channel=Channel.SMS,
                title="Alert",
                body="Test message",
            ),
            channel=Channel.SMS,
            attempt=1,
        )
        handler._tracker.record(
            task.notification_id, task.channel, NotificationStatus.CREATED
        )
        handler._tracker.record(
            task.notification_id, task.channel, NotificationStatus.QUEUED
        )
        handler._tracker.record(
            task.notification_id, task.channel, NotificationStatus.SENDING
        )

        error = DeliveryError(
            notification_id="notif-002",
            channel="sms",
            reason="Provider unavailable",
            retryable=True,
        )

        with patch("notification_system.retry.asyncio.sleep", side_effect=_noop_sleep):
            await handler.handle_failure(task, error)

        retry_task = queue.dequeue_nowait(Channel.SMS)
        assert retry_task is not None
        assert retry_task.attempt == 2


class TestHandleFailureNonRetryable:
    """Tests for handle_failure with non-retryable errors."""

    async def test_non_retryable_error_marks_failed(self, handler, queue, tracker, sample_task):
        """A non-retryable error should mark the notification as FAILED immediately."""
        # Set up valid state for FAILED transition
        tracker.record(
            sample_task.notification_id, sample_task.channel, NotificationStatus.CREATED
        )
        tracker.record(
            sample_task.notification_id, sample_task.channel, NotificationStatus.QUEUED
        )
        tracker.record(
            sample_task.notification_id, sample_task.channel, NotificationStatus.SENDING
        )

        error = DeliveryError(
            notification_id="notif-001",
            channel="email",
            reason="Invalid recipient token",
            retryable=False,
        )

        await handler.handle_failure(sample_task, error)

        # Should NOT re-enqueue
        assert queue.is_empty(Channel.EMAIL)
        # Should be marked as FAILED in tracker
        status = tracker.get_current_status("notif-001")
        assert status == NotificationStatus.FAILED

    async def test_non_retryable_error_no_sleep(self, handler, sample_task, tracker):
        """Non-retryable errors should not wait/sleep before marking failed."""
        tracker.record(
            sample_task.notification_id, sample_task.channel, NotificationStatus.CREATED
        )
        tracker.record(
            sample_task.notification_id, sample_task.channel, NotificationStatus.QUEUED
        )
        tracker.record(
            sample_task.notification_id, sample_task.channel, NotificationStatus.SENDING
        )

        error = DeliveryError(
            notification_id="notif-001",
            channel="email",
            reason="Permanent rejection",
            retryable=False,
        )

        sleep_called = []

        async def _tracking_sleep(*args, **kwargs):
            sleep_called.append(args)

        with patch("notification_system.retry.asyncio.sleep", side_effect=_tracking_sleep):
            await handler.handle_failure(sample_task, error)
        assert len(sleep_called) == 0


class TestHandleFailureMaxRetriesExceeded:
    """Tests for handle_failure when max retries are exceeded."""

    async def test_max_retries_exceeded_marks_failed(self, handler, queue, tracker):
        """When attempt >= max_retries, should mark FAILED and not re-enqueue."""
        task = NotificationTask(
            notification_id="notif-003",
            request=NotificationRequest(
                recipient_id="user-3",
                channel=Channel.IOS_PUSH,
                title="Push",
                body="Retry exhausted",
            ),
            channel=Channel.IOS_PUSH,
            attempt=3,  # max_retries is 3, so attempt >= max_retries
        )
        tracker.record(
            task.notification_id, task.channel, NotificationStatus.CREATED
        )
        tracker.record(
            task.notification_id, task.channel, NotificationStatus.QUEUED
        )
        tracker.record(
            task.notification_id, task.channel, NotificationStatus.SENDING
        )

        error = DeliveryError(
            notification_id="notif-003",
            channel="ios_push",
            reason="Timeout",
            retryable=True,
        )

        await handler.handle_failure(task, error)

        # Should NOT re-enqueue
        assert queue.is_empty(Channel.IOS_PUSH)
        # Should be marked as FAILED
        status = tracker.get_current_status("notif-003")
        assert status == NotificationStatus.FAILED

    async def test_max_retries_exceeded_no_sleep(self, handler, tracker):
        """When max retries exceeded, should not sleep before marking failed."""
        task = NotificationTask(
            notification_id="notif-004",
            request=NotificationRequest(
                recipient_id="user-4",
                channel=Channel.ANDROID_PUSH,
                title="Push",
                body="Max retries",
            ),
            channel=Channel.ANDROID_PUSH,
            attempt=5,  # Well beyond max_retries=3
        )
        tracker.record(
            task.notification_id, task.channel, NotificationStatus.CREATED
        )
        tracker.record(
            task.notification_id, task.channel, NotificationStatus.QUEUED
        )
        tracker.record(
            task.notification_id, task.channel, NotificationStatus.SENDING
        )

        error = DeliveryError(
            notification_id="notif-004",
            channel="android_push",
            reason="Service down",
            retryable=True,
        )

        sleep_called = []

        async def _tracking_sleep(*args, **kwargs):
            sleep_called.append(args)

        with patch("notification_system.retry.asyncio.sleep", side_effect=_tracking_sleep):
            await handler.handle_failure(task, error)
        assert len(sleep_called) == 0
