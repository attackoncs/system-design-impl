"""Unit tests for the MessageQueue."""

from __future__ import annotations

import asyncio

import pytest

from news_feed_system.models import FanoutTask
from news_feed_system.queue import MessageQueue


@pytest.mark.asyncio
async def test_enqueue_dequeue_fifo_order() -> None:
    """Tasks are dequeued in FIFO order."""
    queue = MessageQueue(max_size=10)

    task1 = FanoutTask(post_id="p1", author_id="a1", target_user_ids=["u1"])
    task2 = FanoutTask(post_id="p2", author_id="a2", target_user_ids=["u2"])
    task3 = FanoutTask(post_id="p3", author_id="a3", target_user_ids=["u3"])

    await queue.enqueue(task1)
    await queue.enqueue(task2)
    await queue.enqueue(task3)

    assert await queue.dequeue() == task1
    assert await queue.dequeue() == task2
    assert await queue.dequeue() == task3


@pytest.mark.asyncio
async def test_size_reflects_current_items() -> None:
    """size() returns the current number of items in the queue."""
    queue = MessageQueue(max_size=10)

    assert queue.size() == 0

    task = FanoutTask(post_id="p1", author_id="a1", target_user_ids=["u1"])
    await queue.enqueue(task)
    assert queue.size() == 1

    await queue.enqueue(FanoutTask(post_id="p2", author_id="a1", target_user_ids=["u2"]))
    assert queue.size() == 2

    await queue.dequeue()
    assert queue.size() == 1


@pytest.mark.asyncio
async def test_is_full_with_max_size() -> None:
    """is_full() returns True when queue reaches max size."""
    queue = MessageQueue(max_size=2)

    assert queue.is_full() is False

    await queue.enqueue(FanoutTask(post_id="p1", author_id="a1", target_user_ids=["u1"]))
    assert queue.is_full() is False

    await queue.enqueue(FanoutTask(post_id="p2", author_id="a1", target_user_ids=["u2"]))
    assert queue.is_full() is True

    await queue.dequeue()
    assert queue.is_full() is False


@pytest.mark.asyncio
async def test_is_full_unlimited_queue() -> None:
    """is_full() always returns False for unlimited queues (max_size=0)."""
    queue = MessageQueue(max_size=0)

    assert queue.is_full() is False

    for i in range(100):
        await queue.enqueue(
            FanoutTask(post_id=f"p{i}", author_id="a1", target_user_ids=["u1"])
        )

    assert queue.is_full() is False


@pytest.mark.asyncio
async def test_dequeue_blocks_until_item_available() -> None:
    """dequeue() blocks when queue is empty and resumes when item is enqueued."""
    queue = MessageQueue(max_size=10)
    result: list[FanoutTask] = []

    async def consumer() -> None:
        item = await queue.dequeue()
        result.append(item)

    task = FanoutTask(post_id="p1", author_id="a1", target_user_ids=["u1"])

    consumer_task = asyncio.create_task(consumer())
    # Give the consumer a chance to block
    await asyncio.sleep(0.01)
    assert result == []

    await queue.enqueue(task)
    await consumer_task

    assert result == [task]


@pytest.mark.asyncio
async def test_enqueue_blocks_when_full() -> None:
    """enqueue() blocks when queue is full and resumes when space is available."""
    queue = MessageQueue(max_size=1)

    task1 = FanoutTask(post_id="p1", author_id="a1", target_user_ids=["u1"])
    task2 = FanoutTask(post_id="p2", author_id="a1", target_user_ids=["u2"])

    await queue.enqueue(task1)
    enqueued = False

    async def producer() -> None:
        nonlocal enqueued
        await queue.enqueue(task2)
        enqueued = True

    producer_task = asyncio.create_task(producer())
    await asyncio.sleep(0.01)
    assert enqueued is False

    # Free up space
    dequeued = await queue.dequeue()
    await producer_task

    assert enqueued is True
    assert dequeued == task1
    assert await queue.dequeue() == task2


@pytest.mark.asyncio
async def test_empty_queue_size_zero() -> None:
    """A new queue has size 0."""
    queue = MessageQueue(max_size=100)
    assert queue.size() == 0
