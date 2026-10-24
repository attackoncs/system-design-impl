"""Lifecycle races must not strand accepted publications or leave workers alive."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from news_feed_system.api import NewsFeedAPI
from news_feed_system.models import FeedRequest


async def test_concurrent_start_stop_is_idempotent_and_restartable():
    api = NewsFeedAPI()
    await asyncio.gather(*(api.start() for _ in range(10)))
    assert api.workers.is_running
    await asyncio.gather(*(api.stop() for _ in range(10)))
    assert not api.workers.is_running
    await api.start()
    assert api.workers.is_running
    await api.stop()


async def test_shutdown_waits_for_admitted_publication_and_rejects_new_ones():
    api = NewsFeedAPI()
    await api.start()
    entered, release = asyncio.Event(), asyncio.Event()

    async def publish(request):
        entered.set()
        await release.wait()
        assert api.workers.is_running
        return "accepted"

    api.publisher.publish = publish
    publishing = asyncio.create_task(
        api.publish(FeedRequest("author", "token", "body"))
    )
    await entered.wait()
    closing = asyncio.create_task(api.stop())
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="stopping"):
        await api.publish(FeedRequest("author", "token", "late"))
    assert not closing.done()
    release.set()
    assert await publishing == "accepted"
    await closing
    assert not api.workers.is_running


async def test_cancelled_stop_waiter_keeps_shared_shutdown_owned():
    api = NewsFeedAPI()
    await api.start()
    draining, release = asyncio.Event(), asyncio.Event()

    async def join():
        draining.set()
        await release.wait()

    api.queue.join = join
    first = asyncio.create_task(api.stop())
    await draining.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(api.stop())
    assert api.workers.is_running
    release.set()
    await second
    assert not api.workers.is_running
    await api.start()
    await api.stop()


async def test_cancelled_publication_releases_shutdown_barrier():
    api = NewsFeedAPI()
    entered = asyncio.Event()

    async def publish(request):
        entered.set()
        await asyncio.Event().wait()

    api.publisher.publish = publish
    task = asyncio.create_task(api.publish(FeedRequest("author", "token", "body")))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(api.stop(), timeout=2)
    assert not api.workers.is_running


async def test_api_publication_accepts_before_slow_fanout():
    api = NewsFeedAPI()
    api.auth.register_token("token", "author")
    # Own recovery is the only dispatcher; acceptance must not await this hook.
    api.fanout.fanout = AsyncMock(side_effect=RuntimeError("not ready"))
    try:
        post_id = await asyncio.wait_for(
            api.publish(FeedRequest("author", "token", "body")), 2
        )
        assert api.post_store.get(post_id) is not None
    finally:
        await api.stop()
