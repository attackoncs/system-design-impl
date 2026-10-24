"""Bounded notification validation and privacy during dependency waits."""

import asyncio
import threading

import pytest

from news_feed_system.fanout_service import FanoutService
from news_feed_system.models import Post
from news_feed_system.notification_service import NotificationService
from news_feed_system.post_cache import PostCache
from news_feed_system.queue import MessageQueue
from news_feed_system.social_graph import InMemorySocialGraph
from news_feed_system.social_graph_cache import CachedSocialGraph


@pytest.mark.asyncio
async def test_unfollow_during_post_fetch_prevents_notification():
    graph = CachedSocialGraph(InMemorySocialGraph())
    await graph.add_follow("reader", "author")
    started = threading.Event()
    release = threading.Event()

    class SlowPostCache:
        def get(self, post_id):
            started.set()
            assert release.wait(timeout=3)
            return Post(post_id=post_id, author_id="author", content="post")

    notifications = NotificationService(graph, post_cache=SlowPostCache())
    dispatch = asyncio.create_task(notifications.notify_followers("author", "post"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        await graph.remove_follow("reader", "author")
    finally:
        release.set()
        await asyncio.wait_for(dispatch, 1)
    assert notifications.get_notifications("reader") == []


@pytest.mark.asyncio
async def test_validation_uses_fixed_bounded_tasks_and_shared_privacy_policy():
    class SlowGraph:
        def __init__(self):
            self.active = 0
            self.maximum = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.tasks = set()

        async def get_followers(self, author_id):
            return [f"reader{i}" for i in range(100)]

        async def is_following(self, follower_id, author_id):
            self.tasks.add(asyncio.current_task())
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            if self.active == 4:
                self.started.set()
            try:
                await self.release.wait()
                return True
            finally:
                self.active -= 1

    graph = SlowGraph()
    posts = PostCache()
    posts.put(
        Post(
            post_id="private",
            author_id="author",
            content="post",
            selective_sharing=["reader0", "reader1"],
        )
    )
    policy = FanoutService(graph, posts, MessageQueue())
    policy.add_mute("reader1", "author")
    notifications = NotificationService(
        graph, post_cache=posts, visibility=policy.can_view, concurrency=4
    )
    dispatch = asyncio.create_task(notifications.notify_followers("author", "private"))
    try:
        await asyncio.wait_for(graph.started.wait(), 1)
        assert graph.active == 4
    finally:
        graph.release.set()
        await asyncio.wait_for(dispatch, 1)
    assert graph.maximum == 4
    assert len(graph.tasks) == 4
    assert notifications.get_notifications("reader0") == [("author", "private")]
    for reader in range(1, 100):
        assert notifications.get_notifications(f"reader{reader}") == []
    # At least-once publication recovery must not create duplicate alerts.
    await notifications.notify_followers("author", "private")
    assert notifications.get_notifications("reader0") == [("author", "private")]


@pytest.mark.asyncio
async def test_validation_cancellation_drains_every_child():
    class BlockingGraph:
        def __init__(self):
            self.active = 0
            self.started = asyncio.Event()

        async def get_followers(self, author_id):
            return [f"reader{i}" for i in range(100)]

        async def is_following(self, follower_id, author_id):
            self.active += 1
            if self.active == 3:
                self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.active -= 1

    graph = BlockingGraph()
    service = NotificationService(graph, concurrency=3)
    dispatch = asyncio.create_task(service.notify_followers("author", "post"))
    await asyncio.wait_for(graph.started.wait(), 1)
    dispatch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dispatch
    assert graph.active == 0


@pytest.mark.parametrize("value", [True, 0, -1, 1.5])
def test_notification_validation_concurrency_requires_positive_integer(value):
    with pytest.raises(ValueError, match="concurrency"):
        NotificationService(object(), concurrency=value)
