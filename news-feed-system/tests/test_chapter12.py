"""End-to-end chapter contracts: source recovery, hybrid reads and privacy."""

import asyncio
import dataclasses
import datetime as dt
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest

from news_feed_system import FeedRequest, NewsFeedAPI, NewsFeedConfig, Post, UserProfile
from news_feed_system.config import FanoutConfig, RateLimitConfig
from news_feed_system.cursors import decode_cursor
from news_feed_system.exceptions import AuthenticationError, ValidationError
from news_feed_system.models import ActionType, CounterType
from news_feed_system.post_service import PostService
from news_feed_system.rate_limiter import SlidingWindowRateLimiter
from news_feed_system.sqlite_graph import SQLiteSocialGraph
from news_feed_system.storage import InMemoryPostStore, SQLitePostStore

NOW = dt.datetime(2026, 10, 24, tzinfo=dt.timezone.utc)


def make_post(post_id, author="author", seconds=0, **kwargs):
    return Post(
        post_id,
        author,
        post_id,
        created_at=NOW + dt.timedelta(seconds=seconds),
        **kwargs,
    )


def api_with_tokens(path=None, **kwargs):
    api = NewsFeedAPI(database_path=path, **kwargs)
    api.auth.register_token("writer-token", "author")
    api.auth.register_token("reader-token", "reader")
    return api


def immediate_publisher(api):
    return PostService(
        api.auth,
        SlidingWindowRateLimiter(api.config.rate_limit),
        api.posts,
        api.fanout,
        api.notifications,
    )


@pytest.mark.parametrize("persistent", [False, True])
def test_post_source_keeps_immutable_snapshots_and_keyset(tmp_path, persistent):
    store = (
        SQLitePostStore(str(tmp_path / "posts.db"))
        if persistent
        else InMemoryPostStore()
    )
    original = make_post(
        "a", media_urls=["https://cdn.example/a"], selective_sharing=["reader"]
    )
    store.put(original)
    original.media_urls.clear()
    original.selective_sharing.clear()
    assert store.get("a").selective_sharing == ["reader"]
    store.get("a").media_urls.clear()
    assert store.get("a").media_urls == ["https://cdn.example/a"]
    store.put(make_post("b"))
    assert [p.post_id for p in store.get_by_author("author", 1)] == ["b"]
    assert [p.post_id for p in store.get_by_author("author", 1, (NOW, "b"))] == ["a"]
    with pytest.raises(ValueError):
        store.put(dataclasses.replace(store.get("a"), content="overwrite"))
    store.mark_dispatched("a")
    store.put(store.get("a"))
    assert [p.post_id for p in store.pending_posts()] == ["b"]


def test_sqlite_post_id_race_is_atomic(tmp_path):
    path = str(tmp_path / "shared.db")
    stores = [SQLitePostStore(path), SQLitePostStore(path)]

    # Each contender supplies a different payload under one immutable key.
    def insert(i):
        try:
            stores[i % 2].put(dataclasses.replace(make_post("same"), content=str(i)))
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(insert, range(16))) == 1
    assert len(SQLitePostStore(path).pending_posts()) == 1


@pytest.mark.asyncio
async def test_hybrid_hydration_and_stable_cursor_with_new_posts():
    api = api_with_tokens(
        config=NewsFeedConfig(fanout=FanoutConfig(celebrity_threshold=2))
    )
    await api.graph.add_follow("reader", "author")
    await api.graph.add_follow("reader", "celebrity")
    await api.graph.add_follow("other", "celebrity")
    api.users.put(UserProfile("author", "Alice", "https://cdn.example/avatar"))
    api.posts.put(make_post("n1", seconds=1))
    api.posts.put(make_post("n3", seconds=3))
    api.posts.put(
        make_post(
            "c2", "celebrity", seconds=2, media_urls=["https://cdn.example/video"]
        )
    )
    api.actions.record_action("reader", "n3", ActionType.LIKED)
    api.counters.increment("n3", CounterType.LIKE)
    first = await api.retrieve("reader-token", page_size=2)
    assert [e.post.post_id for e in first.entries] == ["n3", "c2"]
    assert first.entries[0].author.username == "Alice"
    assert first.entries[0].actions.liked and first.entries[0].counters.like_count == 1
    assert first.entries[1].post.media_urls == ["https://cdn.example/video"]
    assert first.has_more and first.next_cursor
    api.posts.put(make_post("n4", seconds=4))
    second = await api.retrieve("reader-token", page_size=2, cursor=first.next_cursor)
    assert [e.post.post_id for e in second.entries] == ["n1"]
    assert not second.has_more and second.next_cursor is None


@pytest.mark.asyncio
async def test_privacy_refills_after_many_private_posts_and_rechecks_push():
    api = api_with_tokens()
    await api.graph.add_follow("reader", "author")
    api.posts.put(make_post("public", seconds=-1))
    for i in range(70):
        api.posts.put(
            make_post(f"private{i}", seconds=i, selective_sharing=["someone-else"])
        )
    feed = await api.retrieve("reader-token")
    assert [e.post.post_id for e in feed.entries] == ["public"]
    await api.fanout.fanout(api.posts.get("public"))
    await api.workers.start()
    await api.queue.join()
    assert api.feeds.get_count("reader") == 1
    api.fanout.add_mute("reader", "author")
    assert not (await api.retrieve("reader-token")).entries
    api.fanout.remove_mute("reader", "author")
    await api.graph.remove_follow("reader", "author")
    assert not (await api.retrieve("reader-token")).entries
    await api.workers.stop()


@pytest.mark.asyncio
async def test_restart_recovers_posts_profiles_graph_and_mutes(tmp_path):
    path = str(tmp_path / "feed.db")
    first = api_with_tokens(path)
    await first.graph.add_follow("reader", "author")
    first.users.put(UserProfile("author", "Persistent Alice"))
    first.posts.put(make_post("durable"))
    await first.mute("reader", "author")
    second = api_with_tokens(path)
    assert second.feeds.get_count("reader") == 0
    assert not (await second.retrieve("reader-token")).entries
    await second.unmute("reader", "author")
    feed = await second.retrieve("reader-token")
    assert feed.entries[0].post.post_id == "durable"
    assert feed.entries[0].author.username == "Persistent Alice"
    assert second.feeds.get_count("reader") == 1  # source repair warms the ID cache


@pytest.mark.asyncio
async def test_publish_failure_retains_outbox_and_replay_deduplicates(tmp_path):
    api = api_with_tokens(str(tmp_path / "feed.db"))
    await api.graph.add_follow("reader", "author")
    await api.graph.add_follow("hidden", "author")
    real_fanout = api.fanout.fanout
    publisher = immediate_publisher(api)
    api.fanout.fanout = AsyncMock(side_effect=RuntimeError("broker unavailable"))
    post_id = await publisher.publish(
        FeedRequest("author", "writer-token", "hello", selective_sharing=["reader"])
    )
    assert api.post_store.pending_posts()[0].post_id == post_id
    assert (await api.retrieve("reader-token")).entries[0].post.post_id == post_id
    api.fanout.fanout = real_fanout
    await api.workers.start()
    assert await publisher.recover_pending() == 1
    await api.queue.join()
    await real_fanout(api.posts.get(post_id))  # at-least-once replay
    await api.queue.join()
    assert api.feeds.get_count("reader") == 1
    assert not api.notifications.get_notifications("hidden")
    assert api.notifications.get_notifications("reader") == [("author", post_id)]
    assert not SQLitePostStore(str(tmp_path / "feed.db")).pending_posts()
    await api.workers.stop()


@pytest.mark.asyncio
async def test_publish_snapshot_precedes_auth_await():
    api = api_with_tokens()
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_auth(_):
        entered.set()
        await release.wait()
        return "author"

    api.auth.validate_token = slow_auth
    media = ["https://cdn.example/a"]
    audience = ["reader"]
    task = asyncio.create_task(
        api.publisher.publish(
            FeedRequest("author", "writer-token", "hello", media, audience)
        )
    )
    await entered.wait()
    media.clear()
    audience.clear()
    release.set()
    post = api.posts.get(await task)
    assert post.media_urls == ["https://cdn.example/a"] and post.selective_sharing == [
        "reader"
    ]


@pytest.mark.asyncio
async def test_durable_follow_limit_is_atomic(tmp_path):
    path = str(tmp_path / "graph.db")
    first, second = SQLiteSocialGraph(path, 1), SQLiteSocialGraph(path, 1)
    results = await asyncio.gather(
        first.add_follow("reader", "a"),
        second.add_follow("reader", "b"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert len(await first.get_following("reader")) == 1


@pytest.mark.asyncio
async def test_tokens_cursor_and_page_input_validation():
    api = api_with_tokens()
    with pytest.raises(AuthenticationError):
        await api.retrieve("wrong")
    with pytest.raises(AuthenticationError):
        await api.publisher.publish(FeedRequest("spoofed-user", "writer-token", "hi"))
    for value in [0, -1, 101, True, "10", 1.5]:
        with pytest.raises(ValidationError):
            await api.retrieve("reader-token", page_size=value)
    for value in ["invalid", "W10", "", "bnVsbA", "a" * 1025]:
        with pytest.raises(ValidationError):
            decode_cursor(value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_posts": True},
        {"window_seconds": float("nan")},
        {"window_seconds": float("inf")},
    ],
)
def test_config_rejects_nonfinite_or_boolean_values(kwargs):
    with pytest.raises(ValueError):
        RateLimitConfig(**kwargs)


@pytest.mark.asyncio
async def test_api_context_owns_workers_and_recovery():
    api = api_with_tokens()
    await api.graph.add_follow("reader", "author")
    async with api:
        post_id = await api.publish(FeedRequest("author", "writer-token", "context"))
        await api.publisher.recover_pending()
        await api.queue.join()
        assert api.feeds.get_entries("reader")[0].post_id == post_id
    assert not api.workers.is_running


@pytest.mark.asyncio
async def test_warm_normal_feed_uses_push_cache_and_celebrity_stays_pull():
    api = api_with_tokens(
        config=NewsFeedConfig(fanout=FanoutConfig(celebrity_threshold=2))
    )
    await api.graph.add_follow("reader", "author")
    await api.graph.add_follow("reader", "celebrity")
    await api.graph.add_follow("other", "celebrity")
    for i in range(4):
        api.posts.put(make_post(f"p{i}", seconds=i))
    api.posts.put(make_post("celebrity", "celebrity", seconds=4))
    await api.retrieve("reader-token", page_size=2)
    calls = []
    get_by_author = api.posts.get_by_author

    def counted(author, *args):
        calls.append(author)
        return get_by_author(author, *args)

    api.posts.get_by_author = counted
    result = await api.retrieve("reader-token", page_size=2)
    assert [e.post.post_id for e in result.entries] == ["celebrity", "p3"]
    assert calls == ["celebrity"]
    # Cursor history uses the source so gaps below a repaired window cannot leak.
    calls.clear()
    await api.retrieve("reader-token", page_size=2, cursor=result.next_cursor)
    assert set(calls) == {"author", "celebrity"}


@pytest.mark.parametrize("persistent", [False, True])
def test_failed_outbox_rows_do_not_starve_new_posts(tmp_path, persistent):
    store = (
        SQLitePostStore(str(tmp_path / "fair.db"))
        if persistent
        else InMemoryPostStore()
    )
    store.put(make_post("poison"))
    store.put(make_post("healthy", seconds=1))
    assert store.pending_posts(1)[0].post_id == "poison"
    store.defer("poison")
    assert store.pending_posts(1)[0].post_id == "healthy"


@pytest.mark.asyncio
async def test_frequent_warm_reads_cannot_postpone_repair(monkeypatch):
    import news_feed_system.news_feed_service as reader_module

    now = [0.0]
    monkeypatch.setattr(reader_module, "monotonic", lambda: now[0])
    api = api_with_tokens()
    await api.graph.add_follow("reader", "author")
    for i in range(3):
        api.posts.put(make_post(f"old{i}", seconds=i))
    await api.retrieve("reader-token", page_size=1)
    api.posts.put(make_post("missed", seconds=10))
    for instant in [10, 20]:
        now[0] = instant
        assert (await api.retrieve("reader-token", page_size=1)).entries[
            0
        ].post.post_id == "old2"
    now[0] = 31
    assert (await api.retrieve("reader-token", page_size=1)).entries[
        0
    ].post.post_id == "missed"


def test_committed_post_and_outbox_survive_process_exit(tmp_path):
    path = str(tmp_path / "crash.db")
    script = """
import os, sys
from news_feed_system.models import Post
from news_feed_system.storage import SQLitePostStore
SQLitePostStore(sys.argv[1]).put(Post('crash', 'author', 'committed'))
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script, path], check=True, timeout=10)
    reopened = SQLitePostStore(path)
    assert reopened.get("crash").content == "committed"
    assert reopened.pending_posts()[0].post_id == "crash"


@pytest.mark.asyncio
async def test_cancelled_publish_drains_commit_and_leaves_recoverable_outbox(tmp_path):
    api = api_with_tokens(str(tmp_path / "cancel.db"))
    entered, release = threading.Event(), threading.Event()
    put = api.post_store.put

    def slow_put(post):
        entered.set()
        assert release.wait(5)
        put(post)

    api.post_store.put = slow_put
    task = asyncio.create_task(
        api.publisher.publish(FeedRequest("author", "writer-token", "committed"))
    )
    while not entered.is_set():
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(api.post_store.pending_posts()) == 1


@pytest.mark.asyncio
async def test_live_dispatch_and_recovery_do_not_duplicate_enqueue():
    api = api_with_tokens()
    entered, release = asyncio.Event(), asyncio.Event()

    async def fanout(post):
        entered.set()
        await release.wait()

    api.fanout.fanout = AsyncMock(side_effect=fanout)
    publisher = immediate_publisher(api)
    live = asyncio.create_task(
        publisher.publish(FeedRequest("author", "writer-token", "once"))
    )
    await entered.wait()
    recovery = asyncio.create_task(publisher.recover_pending())
    await asyncio.sleep(0)
    release.set()
    await live
    assert await recovery == 0
    assert api.fanout.fanout.await_count == 1
