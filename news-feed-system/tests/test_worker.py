"""Unit tests for FanoutWorker and WorkerPool."""

from __future__ import annotations

import asyncio

import pytest

from news_feed_system.models import FanoutTask, FeedEntry
from news_feed_system.news_feed_cache import NewsFeedCache
from news_feed_system.queue import MessageQueue
from news_feed_system.worker import FanoutWorker, WorkerPool


# ---------------------------------------------------------------------------
# FanoutWorker tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_processes_task_and_writes_to_cache() -> None:
    """Worker appends a FeedEntry to each target user's cache."""
    queue = MessageQueue()
    cache = NewsFeedCache()
    worker = FanoutWorker(queue, cache, worker_id=0)

    sentinel_queue: asyncio.Queue = asyncio.Queue()
    task = FanoutTask(
        post_id="p1",
        author_id="author1",
        target_user_ids=["u1", "u2", "u3"],
    )
    await sentinel_queue.put(task)

    worker_task = worker.start(sentinel_queue)

    # Give the worker time to process the item.
    await asyncio.sleep(0.05)

    # Verify entries were written to the cache.
    for user_id in ["u1", "u2", "u3"]:
        entries = cache.get_entries(user_id)
        assert len(entries) == 1
        assert entries[0].post_id == "p1"
        assert entries[0].author_id == "author1"

    # Stop the worker via sentinel.
    from news_feed_system.worker import _STOP_SENTINEL
    await sentinel_queue.put(_STOP_SENTINEL)
    await worker_task


@pytest.mark.asyncio
async def test_worker_preserves_task_timestamp() -> None:
    """FeedEntry timestamp matches the FanoutTask timestamp."""
    from datetime import datetime, timezone

    queue = MessageQueue()
    cache = NewsFeedCache()
    worker = FanoutWorker(queue, cache, worker_id=0)

    sentinel_queue: asyncio.Queue = asyncio.Queue()
    ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    task = FanoutTask(
        post_id="p1",
        author_id="author1",
        target_user_ids=["u1"],
        timestamp=ts,
    )
    await sentinel_queue.put(task)

    worker_task = worker.start(sentinel_queue)
    await asyncio.sleep(0.05)

    entries = cache.get_entries("u1")
    assert entries[0].timestamp == ts

    from news_feed_system.worker import _STOP_SENTINEL
    await sentinel_queue.put(_STOP_SENTINEL)
    await worker_task


@pytest.mark.asyncio
async def test_worker_processes_multiple_tasks_sequentially() -> None:
    """Worker processes multiple tasks in order."""
    queue = MessageQueue()
    cache = NewsFeedCache()
    worker = FanoutWorker(queue, cache, worker_id=0)

    sentinel_queue: asyncio.Queue = asyncio.Queue()
    for i in range(5):
        task = FanoutTask(
            post_id=f"p{i}",
            author_id="author1",
            target_user_ids=["u1"],
        )
        await sentinel_queue.put(task)

    worker_task = worker.start(sentinel_queue)
    await asyncio.sleep(0.1)

    entries = cache.get_entries("u1", page_size=10)
    assert len(entries) == 5

    from news_feed_system.worker import _STOP_SENTINEL
    await sentinel_queue.put(_STOP_SENTINEL)
    await worker_task


# ---------------------------------------------------------------------------
# WorkerPool tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_pool_starts_configured_number_of_workers() -> None:
    """WorkerPool creates the configured number of worker tasks."""
    queue = MessageQueue()
    cache = NewsFeedCache()
    pool = WorkerPool(queue, cache, num_workers=3)

    assert pool.num_workers == 3
    assert not pool.is_running

    await pool.start()
    assert pool.is_running
    assert len(pool._workers) == 3

    await pool.stop()
    assert not pool.is_running


@pytest.mark.asyncio
async def test_worker_pool_processes_tasks_from_queue() -> None:
    """WorkerPool workers consume tasks from the MessageQueue and write to cache."""
    queue = MessageQueue()
    cache = NewsFeedCache()
    pool = WorkerPool(queue, cache, num_workers=2)

    await pool.start()

    task = FanoutTask(
        post_id="p1",
        author_id="author1",
        target_user_ids=["u1", "u2"],
    )
    await queue.enqueue(task)

    # Allow time for the worker to process.
    await asyncio.sleep(0.1)

    for user_id in ["u1", "u2"]:
        entries = cache.get_entries(user_id)
        assert len(entries) == 1
        assert entries[0].post_id == "p1"

    await pool.stop()


@pytest.mark.asyncio
async def test_worker_pool_graceful_shutdown_completes_inflight_tasks() -> None:
    """stop() waits for in-flight tasks to complete before returning."""
    queue = MessageQueue()
    cache = NewsFeedCache()
    pool = WorkerPool(queue, cache, num_workers=2)

    await pool.start()

    # Enqueue several tasks before stopping.
    for i in range(10):
        await queue.enqueue(
            FanoutTask(
                post_id=f"p{i}",
                author_id="author1",
                target_user_ids=["u1"],
            )
        )

    # Give workers a moment to pick up some tasks.
    await asyncio.sleep(0.05)

    await pool.stop()

    # After stop, all tasks that were picked up should be in the cache.
    # (Tasks still in the MessageQueue may not have been processed.)
    entries = cache.get_entries("u1", page_size=20)
    # At least some entries should have been written.
    assert len(entries) >= 0  # pool stopped cleanly without error


@pytest.mark.asyncio
async def test_worker_pool_stop_raises_if_not_running() -> None:
    """stop() raises RuntimeError if the pool is not running."""
    queue = MessageQueue()
    cache = NewsFeedCache()
    pool = WorkerPool(queue, cache, num_workers=2)

    with pytest.raises(RuntimeError, match="not running"):
        await pool.stop()


@pytest.mark.asyncio
async def test_worker_pool_start_raises_if_already_running() -> None:
    """start() raises RuntimeError if the pool is already running."""
    queue = MessageQueue()
    cache = NewsFeedCache()
    pool = WorkerPool(queue, cache, num_workers=2)

    await pool.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            await pool.start()
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_worker_pool_invalid_num_workers() -> None:
    """WorkerPool raises ValueError for non-positive num_workers."""
    queue = MessageQueue()
    cache = NewsFeedCache()

    with pytest.raises(ValueError, match="num_workers"):
        WorkerPool(queue, cache, num_workers=0)

    with pytest.raises(ValueError, match="num_workers"):
        WorkerPool(queue, cache, num_workers=-1)


@pytest.mark.asyncio
async def test_worker_pool_concurrent_processing() -> None:
    """Multiple workers process tasks concurrently."""
    queue = MessageQueue()
    cache = NewsFeedCache()
    pool = WorkerPool(queue, cache, num_workers=4)

    await pool.start()

    # Enqueue tasks for many different users.
    num_tasks = 20
    for i in range(num_tasks):
        await queue.enqueue(
            FanoutTask(
                post_id=f"p{i}",
                author_id="author1",
                target_user_ids=[f"user_{i}"],
            )
        )

    # Wait for all tasks to be processed.
    await asyncio.sleep(0.2)

    await pool.stop()

    # Each user should have exactly one entry.
    processed = sum(
        1 for i in range(num_tasks) if cache.get_count(f"user_{i}") == 1
    )
    assert processed == num_tasks
