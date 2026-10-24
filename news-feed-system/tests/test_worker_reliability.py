"""Regression coverage for queue pressure, partial batches and worker recovery."""

import asyncio

import pytest

from news_feed_system.fanout_service import FanoutService
from news_feed_system.models import FanoutTask, Post
from news_feed_system.news_feed_cache import NewsFeedCache
from news_feed_system.post_cache import PostCache
from news_feed_system.queue import MessageQueue
from news_feed_system.worker import WorkerPool


def job(post_id="p1", readers=None):
    return FanoutTask(
        post_id=post_id, author_id="author", target_user_ids=readers or ["reader"]
    )


@pytest.mark.asyncio
async def test_claim_retains_capacity_and_retry_reuses_it():
    queue = MessageQueue(max_size=1)
    await queue.enqueue(job())
    first = await queue.claim()
    producer = asyncio.create_task(queue.enqueue(job("p2")))
    await asyncio.sleep(0)
    assert queue.is_full() and not producer.done()
    queue.retry(first)
    second = await queue.claim()
    assert second.attempts == 2
    with pytest.raises(ValueError, match="stale"):
        queue.ack(first)
    queue.ack(second)
    await asyncio.wait_for(producer, 1)
    assert (await queue.dequeue()).post_id == "p2"
    await asyncio.wait_for(queue.join(), 1)


@pytest.mark.asyncio
async def test_delayed_retry_does_not_block_ready_job():
    queue = MessageQueue(max_size=2)
    await queue.enqueue(job("retry"))
    claim = await queue.claim()
    queue.retry(claim, delay_seconds=0.02)
    await queue.enqueue(job("ready"))
    assert (await queue.dequeue()).post_id == "ready"
    assert (await queue.dequeue()).post_id == "retry"


@pytest.mark.asyncio
async def test_queue_captures_mutable_target_list_on_acceptance():
    queue = MessageQueue()
    task = job(readers=["reader"])
    await queue.enqueue(task)
    # Legacy caller code can still pass mutable lists to frozen dataclasses.
    if isinstance(task.target_user_ids, list):
        task.target_user_ids.append("surprise")
    assert tuple((await queue.dequeue()).target_user_ids) == ("reader",)


class PausingCache(NewsFeedCache):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def append(self, user_id, entry):
        self.started.set()
        await self.release.wait()
        super().append(user_id, entry)


@pytest.mark.asyncio
async def test_workers_consume_directly_without_bypassing_queue_capacity():
    queue = MessageQueue(max_size=2)
    cache = PausingCache()
    await queue.enqueue(job("p1"))
    await queue.enqueue(job("p2"))
    pool = WorkerPool(queue, cache, num_workers=1)
    await pool.start()
    await asyncio.wait_for(cache.started.wait(), 1)
    producer = asyncio.create_task(queue.enqueue(job("p3")))
    try:
        await asyncio.sleep(0)
        assert queue.size() == 1
        assert queue.in_flight() == 1
        assert not producer.done()
        cache.release.set()
        await asyncio.wait_for(producer, 1)
        await asyncio.wait_for(queue.join(), 1)
        assert cache.get_count("reader") == 3
    finally:
        cache.release.set()
        await pool.stop()
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)


class FailOnceCache(NewsFeedCache):
    def __init__(self):
        super().__init__()
        self.fail = True

    def append(self, user_id, entry):
        if user_id == "second" and self.fail:
            self.fail = False
            raise OSError("transient cache error")
        super().append(user_id, entry)


@pytest.mark.asyncio
async def test_partial_batch_retries_idempotently_and_worker_survives():
    queue = MessageQueue(max_size=2)
    cache = FailOnceCache()
    pool = WorkerPool(queue, cache, num_workers=1, retry_delay_seconds=0.001)
    await queue.enqueue(job("retry", ["first", "second"]))
    await queue.enqueue(job("healthy"))
    await pool.start()
    try:
        await asyncio.wait_for(queue.join(), 1)
        assert cache.get_count("first") == 1
        assert cache.get_count("second") == 1
        assert cache.get_count("reader") == 1
        assert not queue.dead_letters
        assert all(not worker.task.done() for worker in pool._workers)
    finally:
        await pool.stop()


class BrokenPostCache(NewsFeedCache):
    def append(self, user_id, entry):
        if entry.post_id.startswith("bad"):
            raise OSError("permanent cache error")
        super().append(user_id, entry)


@pytest.mark.asyncio
async def test_exhausted_retry_is_visible_and_dead_letters_remain_bounded():
    queue = MessageQueue(max_size=3, dead_letter_capacity=1)
    cache = BrokenPostCache()
    pool = WorkerPool(
        queue, cache, num_workers=1, max_retries=1, retry_delay_seconds=0.001
    )
    for post in ("bad1", "bad2", "healthy"):
        await queue.enqueue(job(post))
    await pool.start()
    try:
        await asyncio.wait_for(queue.join(), 1)
        assert queue.failed_count == 2
        assert len(queue.dead_letters) == 1
        assert queue.dead_letters[0].attempts == 2
        assert "permanent cache error" in queue.dead_letters[0].error
        assert cache.get_entries("reader")[0].post_id == "healthy"
    finally:
        await pool.stop()


class CancelOnceCache(NewsFeedCache):
    def __init__(self):
        super().__init__()
        self.block_once = True
        self.started = asyncio.Event()

    async def append(self, user_id, entry):
        if user_id == "second" and self.block_once:
            self.block_once = False
            self.started.set()
            await asyncio.Event().wait()
        super().append(user_id, entry)


@pytest.mark.asyncio
async def test_forced_shutdown_returns_partial_claim_for_restart():
    queue = MessageQueue(max_size=1)
    cache = CancelOnceCache()
    pool = WorkerPool(queue, cache, num_workers=1)
    await queue.enqueue(job(readers=["first", "second"]))
    await pool.start()
    await asyncio.wait_for(cache.started.wait(), 1)
    await asyncio.wait_for(pool.stop(timeout=0.01), 1)
    assert queue.size() == 1 and queue.in_flight() == 0
    await pool.start()
    try:
        await asyncio.wait_for(queue.join(), 1)
        assert cache.get_count("first") == 1
        assert cache.get_count("second") == 1
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_cancel_after_claim_before_worker_observes_it_returns_job():
    class RaceQueue(MessageQueue):
        on_claim = None

        async def claim(self):
            value = await super().claim()
            if self.on_claim:
                asyncio.get_running_loop().call_soon(self.on_claim)
            return value

    queue = RaceQueue(max_size=1)
    cache = NewsFeedCache()
    await queue.enqueue(job())
    pool = WorkerPool(queue, cache, num_workers=1)
    await pool.start()
    queue.on_claim = pool._workers[0].task.cancel
    await asyncio.sleep(0.02)
    await pool.stop()
    assert queue.size() == 1 and queue.in_flight() == 0
    queue.on_claim = None
    await pool.start()
    try:
        await asyncio.wait_for(queue.join(), 1)
        assert cache.get_count("reader") == 1
    finally:
        await pool.stop()


def test_shared_visibility_policy_checks_current_mutes_and_selective_sharing():
    service = FanoutService(object(), PostCache(), MessageQueue())
    public = Post(post_id="public", author_id="author", content="post")
    private = Post(
        post_id="private",
        author_id="author",
        content="post",
        selective_sharing=["allowed"],
    )
    assert service.can_view("reader", public)
    assert not service.can_view("reader", private)
    assert service.can_view("allowed", private)
    service.add_mute("allowed", "author")
    assert not service.can_view("allowed", private)
    service.remove_mute("allowed", "author")
    assert service.can_view("allowed", private)
