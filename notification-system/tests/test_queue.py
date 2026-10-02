"""Unit tests for the MessageQueue module."""

import asyncio

import pytest

from notification_system.exceptions import QueueFullError
from notification_system.models import Channel, NotificationRequest, NotificationTask
from notification_system.queue import MessageQueue


def _make_task(channel: Channel, notification_id: str = "notif-1") -> NotificationTask:
    """Helper to create a NotificationTask for testing."""
    request = NotificationRequest(
        recipient_id="user1",
        channel=channel,
        title="Test",
        body="Test body",
    )
    return NotificationTask(
        notification_id=notification_id,
        request=request,
        channel=channel,
    )


class TestEnqueueDequeue:
    """Tests for basic enqueue and dequeue operations."""

    @pytest.mark.asyncio
    async def test_enqueue_and_dequeue_single_task(self):
        mq = MessageQueue(max_depth=10)
        task = _make_task(Channel.EMAIL)
        await mq.enqueue(task)
        result = await mq.dequeue(Channel.EMAIL)
        assert result is task

    @pytest.mark.asyncio
    async def test_enqueue_multiple_and_dequeue_all(self):
        mq = MessageQueue(max_depth=10)
        tasks = [_make_task(Channel.SMS, f"notif-{i}") for i in range(5)]
        for t in tasks:
            await mq.enqueue(t)

        results = []
        for _ in range(5):
            results.append(await mq.dequeue(Channel.SMS))
        assert results == tasks

    @pytest.mark.asyncio
    async def test_dequeue_nowait_returns_task_when_available(self):
        mq = MessageQueue(max_depth=10)
        task = _make_task(Channel.IOS_PUSH)
        await mq.enqueue(task)
        result = mq.dequeue_nowait(Channel.IOS_PUSH)
        assert result is task

    @pytest.mark.asyncio
    async def test_dequeue_nowait_returns_none_when_empty(self):
        mq = MessageQueue(max_depth=10)
        result = mq.dequeue_nowait(Channel.EMAIL)
        assert result is None


class TestFIFOOrdering:
    """Tests for FIFO ordering within each channel queue."""

    @pytest.mark.asyncio
    async def test_fifo_order_preserved(self):
        mq = MessageQueue(max_depth=100)
        tasks = [_make_task(Channel.ANDROID_PUSH, f"notif-{i}") for i in range(10)]
        for t in tasks:
            await mq.enqueue(t)

        results = []
        for _ in range(10):
            results.append(await mq.dequeue(Channel.ANDROID_PUSH))

        assert results == tasks

    @pytest.mark.asyncio
    async def test_fifo_order_with_interleaved_enqueue_dequeue(self):
        mq = MessageQueue(max_depth=100)
        task1 = _make_task(Channel.EMAIL, "notif-1")
        task2 = _make_task(Channel.EMAIL, "notif-2")
        task3 = _make_task(Channel.EMAIL, "notif-3")

        await mq.enqueue(task1)
        await mq.enqueue(task2)
        result1 = await mq.dequeue(Channel.EMAIL)
        await mq.enqueue(task3)
        result2 = await mq.dequeue(Channel.EMAIL)
        result3 = await mq.dequeue(Channel.EMAIL)

        assert result1 is task1
        assert result2 is task2
        assert result3 is task3


class TestMaxDepthEnforcement:
    """Tests for max depth configuration and enforcement."""

    @pytest.mark.asyncio
    async def test_enqueue_up_to_max_depth_succeeds(self):
        mq = MessageQueue(max_depth=5)
        for i in range(5):
            await mq.enqueue(_make_task(Channel.EMAIL, f"notif-{i}"))
        assert mq.depth(Channel.EMAIL) == 5

    @pytest.mark.asyncio
    async def test_enqueue_beyond_max_depth_raises_queue_full_error(self):
        mq = MessageQueue(max_depth=3)
        for i in range(3):
            await mq.enqueue(_make_task(Channel.SMS, f"notif-{i}"))

        with pytest.raises(QueueFullError) as exc_info:
            await mq.enqueue(_make_task(Channel.SMS, "notif-overflow"))

        assert exc_info.value.channel == "sms"
        assert exc_info.value.max_depth == 3

    @pytest.mark.asyncio
    async def test_enqueue_succeeds_after_dequeue_frees_space(self):
        mq = MessageQueue(max_depth=2)
        await mq.enqueue(_make_task(Channel.EMAIL, "notif-1"))
        await mq.enqueue(_make_task(Channel.EMAIL, "notif-2"))

        # Queue is full
        with pytest.raises(QueueFullError):
            await mq.enqueue(_make_task(Channel.EMAIL, "notif-3"))

        # Dequeue one to free space
        await mq.dequeue(Channel.EMAIL)

        # Now enqueue should succeed
        await mq.enqueue(_make_task(Channel.EMAIL, "notif-3"))
        assert mq.depth(Channel.EMAIL) == 2

    @pytest.mark.asyncio
    async def test_queue_full_error_message_contains_channel_and_depth(self):
        mq = MessageQueue(max_depth=1)
        await mq.enqueue(_make_task(Channel.IOS_PUSH, "notif-1"))

        with pytest.raises(QueueFullError) as exc_info:
            await mq.enqueue(_make_task(Channel.IOS_PUSH, "notif-2"))

        error_msg = str(exc_info.value)
        assert "ios_push" in error_msg
        assert "1" in error_msg


class TestDepthIsFullIsEmpty:
    """Tests for depth(), is_full(), and is_empty() methods."""

    @pytest.mark.asyncio
    async def test_depth_starts_at_zero(self):
        mq = MessageQueue(max_depth=10)
        for channel in Channel:
            assert mq.depth(channel) == 0

    @pytest.mark.asyncio
    async def test_depth_increases_on_enqueue(self):
        mq = MessageQueue(max_depth=10)
        await mq.enqueue(_make_task(Channel.EMAIL, "notif-1"))
        assert mq.depth(Channel.EMAIL) == 1
        await mq.enqueue(_make_task(Channel.EMAIL, "notif-2"))
        assert mq.depth(Channel.EMAIL) == 2

    @pytest.mark.asyncio
    async def test_depth_decreases_on_dequeue(self):
        mq = MessageQueue(max_depth=10)
        await mq.enqueue(_make_task(Channel.EMAIL, "notif-1"))
        await mq.enqueue(_make_task(Channel.EMAIL, "notif-2"))
        await mq.dequeue(Channel.EMAIL)
        assert mq.depth(Channel.EMAIL) == 1

    @pytest.mark.asyncio
    async def test_is_empty_true_initially(self):
        mq = MessageQueue(max_depth=10)
        for channel in Channel:
            assert mq.is_empty(channel) is True

    @pytest.mark.asyncio
    async def test_is_empty_false_after_enqueue(self):
        mq = MessageQueue(max_depth=10)
        await mq.enqueue(_make_task(Channel.SMS))
        assert mq.is_empty(Channel.SMS) is False

    @pytest.mark.asyncio
    async def test_is_empty_true_after_all_dequeued(self):
        mq = MessageQueue(max_depth=10)
        await mq.enqueue(_make_task(Channel.SMS))
        await mq.dequeue(Channel.SMS)
        assert mq.is_empty(Channel.SMS) is True

    @pytest.mark.asyncio
    async def test_is_full_false_initially(self):
        mq = MessageQueue(max_depth=10)
        for channel in Channel:
            assert mq.is_full(channel) is False

    @pytest.mark.asyncio
    async def test_is_full_true_at_max_depth(self):
        mq = MessageQueue(max_depth=3)
        for i in range(3):
            await mq.enqueue(_make_task(Channel.ANDROID_PUSH, f"notif-{i}"))
        assert mq.is_full(Channel.ANDROID_PUSH) is True

    @pytest.mark.asyncio
    async def test_is_full_false_after_dequeue(self):
        mq = MessageQueue(max_depth=2)
        await mq.enqueue(_make_task(Channel.ANDROID_PUSH, "notif-1"))
        await mq.enqueue(_make_task(Channel.ANDROID_PUSH, "notif-2"))
        assert mq.is_full(Channel.ANDROID_PUSH) is True
        await mq.dequeue(Channel.ANDROID_PUSH)
        assert mq.is_full(Channel.ANDROID_PUSH) is False


class TestPerChannelIsolation:
    """Tests for per-channel queue isolation."""

    @pytest.mark.asyncio
    async def test_separate_queues_per_channel(self):
        mq = MessageQueue(max_depth=10)
        email_task = _make_task(Channel.EMAIL, "email-1")
        sms_task = _make_task(Channel.SMS, "sms-1")

        await mq.enqueue(email_task)
        await mq.enqueue(sms_task)

        assert mq.depth(Channel.EMAIL) == 1
        assert mq.depth(Channel.SMS) == 1
        assert mq.depth(Channel.IOS_PUSH) == 0
        assert mq.depth(Channel.ANDROID_PUSH) == 0

    @pytest.mark.asyncio
    async def test_dequeue_from_correct_channel(self):
        mq = MessageQueue(max_depth=10)
        email_task = _make_task(Channel.EMAIL, "email-1")
        sms_task = _make_task(Channel.SMS, "sms-1")

        await mq.enqueue(email_task)
        await mq.enqueue(sms_task)

        result = await mq.dequeue(Channel.EMAIL)
        assert result is email_task
        result = await mq.dequeue(Channel.SMS)
        assert result is sms_task

    @pytest.mark.asyncio
    async def test_full_channel_does_not_affect_others(self):
        mq = MessageQueue(max_depth=2)
        # Fill the EMAIL queue
        await mq.enqueue(_make_task(Channel.EMAIL, "email-1"))
        await mq.enqueue(_make_task(Channel.EMAIL, "email-2"))
        assert mq.is_full(Channel.EMAIL) is True

        # Other channels should still accept tasks
        await mq.enqueue(_make_task(Channel.SMS, "sms-1"))
        assert mq.is_full(Channel.SMS) is False
        assert mq.depth(Channel.SMS) == 1

    @pytest.mark.asyncio
    async def test_all_four_channels_independent(self):
        mq = MessageQueue(max_depth=10)
        channels = list(Channel)
        tasks = {}

        for ch in channels:
            tasks[ch] = _make_task(ch, f"{ch.value}-task")
            await mq.enqueue(tasks[ch])

        for ch in channels:
            result = await mq.dequeue(ch)
            assert result is tasks[ch]
            assert mq.is_empty(ch) is True

    @pytest.mark.asyncio
    async def test_dequeue_blocks_on_empty_channel(self):
        """Verify dequeue blocks when queue is empty and unblocks on enqueue."""
        mq = MessageQueue(max_depth=10)
        task = _make_task(Channel.EMAIL, "delayed-task")

        async def delayed_enqueue():
            await asyncio.sleep(0.05)
            await mq.enqueue(task)

        # Start dequeue (will block) and delayed enqueue concurrently
        dequeue_coro = mq.dequeue(Channel.EMAIL)
        enqueue_task = asyncio.create_task(delayed_enqueue())

        result = await asyncio.wait_for(dequeue_coro, timeout=1.0)
        await enqueue_task

        assert result is task
