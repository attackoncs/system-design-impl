"""Cancellation cannot abandon queue claims or work already running in threads."""

import asyncio
import threading

import pytest
from news_feed_system.concurrency import gather_owned, run_sync
from news_feed_system.models import FanoutTask
from news_feed_system.news_feed_cache import NewsFeedCache
from news_feed_system.queue import MessageQueue
from news_feed_system.worker import FanoutWorker, WorkerPool


def job():
    return FanoutTask("post", "author", ["reader"])


async def cancel_twice(task):
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_run_sync_drains_thread_after_repeated_cancel():
    entered, release, finished = (threading.Event() for _ in range(3))

    def operation():
        entered.set()
        release.wait(5)
        finished.set()

    pending = asyncio.create_task(run_sync(operation))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await cancel_twice(pending)
        assert not pending.done()
        assert not finished.is_set()
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
    assert finished.is_set()


async def test_run_sync_operation_error_wins_over_cancellation():
    entered, release = threading.Event(), threading.Event()

    def operation():
        entered.set()
        release.wait(5)
        raise OSError("disk failed")

    pending = asyncio.create_task(run_sync(operation))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await cancel_twice(pending)
        assert not pending.done()
    finally:
        release.set()
    with pytest.raises(OSError, match="disk failed"):
        await pending


async def test_gather_owned_drains_child_cleanup_after_repeated_cancel():
    started, cleaning, release, finished = (asyncio.Event() for _ in range(4))

    async def child():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            finished.set()

    pending = asyncio.create_task(gather_owned(child()))
    try:
        await asyncio.wait_for(started.wait(), 2)
        pending.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        await cancel_twice(pending)
        assert not pending.done()
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
    assert finished.is_set()


async def test_gather_owned_keeps_first_error_until_other_cleanup_finishes():
    started, cleaning, release, finished = (asyncio.Event() for _ in range(4))

    async def slow():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            finished.set()

    async def fail():
        await started.wait()
        raise OSError("reader failed")

    pending = asyncio.create_task(gather_owned(slow(), fail()))
    try:
        await asyncio.wait_for(cleaning.wait(), 2)
        await cancel_twice(pending)
        assert not pending.done()
    finally:
        release.set()
    with pytest.raises(OSError, match="reader failed"):
        await pending
    assert finished.is_set()


async def test_repeated_cancel_returns_claim_before_worker_exits():
    class RaceQueue(MessageQueue):
        owner = None

        async def claim(self):
            claimed = await super().claim()
            asyncio.get_running_loop().call_soon(self.owner.cancel)
            return claimed

    queue = RaceQueue(1)
    await queue.enqueue(job())
    worker = FanoutWorker(queue, NewsFeedCache())
    pending = asyncio.create_task(worker._next_claim())
    queue.owner = pending
    try:
        await asyncio.sleep(0)
        await cancel_twice(pending)
    finally:
        await asyncio.gather(pending, return_exceptions=True)
    assert queue.in_flight() == 0
    assert queue.size() == 1


async def test_repeated_cancel_keeps_pool_stop_owned_until_worker_cleanup():
    class SlowCache(NewsFeedCache):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.cleaning = asyncio.Event()
            self.release = asyncio.Event()

        async def append(self, user_id, entry):
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cleaning.set()
                await self.release.wait()

    queue, cache = MessageQueue(1), SlowCache()
    await queue.enqueue(job())
    pool = WorkerPool(queue, cache, num_workers=1)
    await pool.start()
    stopping = None
    try:
        await asyncio.wait_for(cache.started.wait(), 2)
        stopping = asyncio.create_task(pool.stop(timeout=0.01))
        await asyncio.wait_for(cache.cleaning.wait(), 2)
        await cancel_twice(stopping)
        assert not stopping.done()
        assert pool.is_running
    finally:
        cache.release.set()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        if pool.is_running:
            await pool.stop(timeout=0.01)
    assert queue.in_flight() == 0
