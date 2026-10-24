"""Regression coverage for duplicate delivery, eviction, races and cache ownership."""

import asyncio
import concurrent.futures
import datetime as dt
import threading

import pytest

from news_feed_system.action_cache import ActionCache
from news_feed_system.config import RateLimitConfig
from news_feed_system.counter_cache import CounterCache
from news_feed_system.exceptions import RateLimitError
from news_feed_system.models import ActionType, CounterType, FeedEntry, Post
from news_feed_system.news_feed_cache import NewsFeedCache
from news_feed_system.post_cache import PostCache
from news_feed_system.rate_limiter import SlidingWindowRateLimiter
from news_feed_system.social_graph import InMemorySocialGraph
from news_feed_system.social_graph_cache import CachedSocialGraph
from news_feed_system.storage import InMemoryPostStore, SQLitePostStore
from news_feed_system.user_cache import UserCache

NOW = dt.datetime(2026, 10, 24, tzinfo=dt.timezone.utc)


def test_late_fanout_and_duplicates_preserve_newest_ids():
    cache = NewsFeedCache(max_entries=3)
    for post_id, seconds in [("b", 2), ("z", 0), ("a", 2), ("c", 1), ("b", 2)]:
        cache.append(
            "reader", FeedEntry(post_id, "author", NOW + dt.timedelta(seconds=seconds))
        )
    assert [entry.post_id for entry in cache.get_entries("reader")] == ["b", "a", "c"]
    assert cache.get_count("reader") == 3


def test_parallel_duplicate_fanout_is_idempotent_and_bounded():
    cache = NewsFeedCache(max_entries=20)

    def deliver(index):
        cache.append("reader", FeedEntry(str(index % 100).zfill(3), "author", NOW))
        cache.get_entries("reader")

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(deliver, range(2000)))
    assert [entry.post_id for entry in cache.get_entries("reader")] == [
        str(i).zfill(3) for i in range(99, 79, -1)
    ]


@pytest.mark.parametrize("persistent", [False, True])
def test_post_cache_eviction_falls_back_without_recreating_outbox(tmp_path, persistent):
    store = (
        SQLitePostStore(str(tmp_path / "posts.sqlite3"))
        if persistent
        else InMemoryPostStore()
    )
    cache = PostCache(hot_capacity=1, normal_capacity=1, store=store)
    for index in range(3):
        cache.put(
            Post(
                str(index),
                "author",
                "body",
                created_at=NOW + dt.timedelta(seconds=index),
            )
        )
        store.mark_dispatched(str(index))
    assert not cache.contains("0")
    assert cache.get("0").post_id == "0"
    assert cache.is_hot("0")
    assert store.pending_posts() == []
    assert [post.post_id for post in cache.get_by_author("author", limit=2)] == [
        "2",
        "1",
    ]
    assert len(cache) <= 2


def test_cache_tier_promotion_and_lru_demotion():
    cache = PostCache(hot_capacity=1, normal_capacity=2, access_threshold=2)
    for post_id in ("a", "b", "c"):
        cache.put(Post(post_id, "author", "body"))
    assert cache.is_normal("a")
    cache.get("a")
    assert cache.is_normal("a")
    cache.get("a")
    assert cache.is_hot("a")
    assert cache.is_normal("c")
    assert len(cache) == 3


def test_cache_storage_failure_does_not_publish_cached_body():
    class FailedStore(InMemoryPostStore):
        def put(self, post):
            raise OSError("disk unavailable")

    cache = PostCache(store=FailedStore())
    with pytest.raises(OSError, match="disk unavailable"):
        cache.put(Post("post", "author", "body"))
    assert not cache.contains("post")


def test_post_lists_are_snapshots_on_accept_and_read():
    cache = PostCache()
    post = Post(
        "post", "author", "body", media_urls=["original"], selective_sharing=["reader"]
    )
    cache.put(post)
    post.media_urls.append("unaccepted")
    post.selective_sharing.clear()
    fetched = cache.get("post")
    assert fetched.media_urls == ["original"]
    assert fetched.selective_sharing == ["reader"]
    fetched.media_urls.clear()
    fetched.selective_sharing.append("stranger")
    assert cache.get("post").media_urls == ["original"]
    assert cache.get("post").selective_sharing == ["reader"]


def test_shared_counters_do_not_lose_parallel_updates():
    cache = CounterCache()

    def increment(_):
        for _ in range(1000):
            cache.increment("post", CounterType.LIKE)

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(increment, range(12)))
    assert cache.get("post", CounterType.LIKE) == 12000


def test_parallel_rate_limit_admission_is_atomic():
    limiter = SlidingWindowRateLimiter(RateLimitConfig(max_posts=7), clock=lambda: 10)
    barrier = threading.Barrier(20)

    def attempt(_):
        barrier.wait(timeout=10)
        try:
            limiter.check_and_record("author")
        except RateLimitError:
            return False
        return True

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        assert sum(executor.map(attempt, range(20))) == 7


def test_rate_limit_exact_boundary_expires():
    now = [0.0]
    limiter = SlidingWindowRateLimiter(
        RateLimitConfig(max_posts=1, window_seconds=10), clock=lambda: now[0]
    )
    limiter.check_and_record("author")
    now[0] = 10
    limiter.check_and_record("author")


def test_action_snapshots_do_not_modify_internal_state():
    cache = ActionCache()
    cache.record_action("reader", "post", ActionType.SHARED)
    cache.get_actions("reader", "post").other_actions.clear()
    assert cache.has_action("reader", "post", ActionType.SHARED)


class PausedGraph(InMemorySocialGraph):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def get_followers(self, user_id):
        self.calls += 1
        result = await super().get_followers(user_id)
        if self.calls == 1:
            self.started.set()
            await self.release.wait()
        return result


async def test_concurrent_graph_misses_coalesce_and_return_detached_lists():
    backend = PausedGraph()
    await backend.add_follow("reader", "author")
    cache = CachedSocialGraph(backend)
    tasks = [asyncio.create_task(cache.get_followers("author")) for _ in range(20)]
    await backend.started.wait()
    backend.release.set()
    results = await asyncio.gather(*tasks)
    assert backend.calls == 1
    results[0].clear()
    assert await cache.get_followers("author") == ["reader"]
    assert results[1] == ["reader"]


async def test_mutation_cannot_be_overwritten_by_old_cache_fill():
    backend = PausedGraph()
    await backend.add_follow("old", "author")
    cache = CachedSocialGraph(backend)
    read = asyncio.create_task(cache.get_followers("author"))
    await backend.started.wait()
    remove = asyncio.create_task(cache.remove_follow("old", "author"))
    backend.release.set()
    await asyncio.gather(read, remove)
    assert await cache.get_followers("author") == []


async def test_manual_invalidation_fences_an_inflight_fill():
    backend = PausedGraph()
    await backend.add_follow("old", "author")
    cache = CachedSocialGraph(backend)
    read = asyncio.create_task(cache.get_followers("author"))
    await backend.started.wait()
    await backend.remove_follow("old", "author")
    cache.invalidate("author")
    backend.release.set()
    assert await read == ["old"]
    assert await cache.get_followers("author") == []
    assert backend.calls == 2


async def test_social_cache_lru_capacity_bounds_stale_relationships():
    backend = PausedGraph()
    backend.release.set()
    cache = CachedSocialGraph(backend, capacity=1)
    await cache.get_followers("a")
    await cache.get_followers("b")
    await backend.add_follow("reader", "a")
    assert await cache.get_followers("a") == ["reader"]


async def test_count_cache_coalesces_and_invalidates_after_mutations():
    class CountingGraph(InMemorySocialGraph):
        calls = 0

        async def get_follower_count(self, user_id):
            self.calls += 1
            await asyncio.sleep(0)
            return await super().get_follower_count(user_id)

    backend = CountingGraph()
    cache = CachedSocialGraph(backend)
    assert (
        await asyncio.gather(*(cache.get_follower_count("author") for _ in range(20)))
        == [0] * 20
    )
    assert backend.calls == 1
    await cache.add_follow("reader", "author")
    assert await cache.get_follower_count("author") == 1
    await cache.remove_follow("reader", "author")
    assert await cache.get_follower_count("author") == 0
    assert backend.calls == 3


async def test_count_cache_ttl_capacity_and_manual_invalidation():
    now = [0.0]
    backend = InMemorySocialGraph()
    cache = CachedSocialGraph(backend, ttl_seconds=10, clock=lambda: now[0], capacity=1)
    assert await cache.get_follower_count("author") == 0
    await backend.add_follow("reader", "author")
    assert await cache.get_follower_count("author") == 0
    now[0] = 10
    assert await cache.get_follower_count("author") == 1
    await cache.get_follower_count("other")
    await backend.add_follow("reader2", "author")
    assert await cache.get_follower_count("author") == 2
    await backend.add_follow("reader3", "author")
    cache.invalidate("author")
    assert await cache.get_follower_count("author") == 3


@pytest.mark.parametrize("invalid", [0, -1, True, 1.2])
def test_cache_capacity_validation(invalid):
    for cache_type in (NewsFeedCache, PostCache, UserCache, ActionCache):
        with pytest.raises(ValueError):
            cache_type(invalid)


@pytest.mark.parametrize("invalid", [0, -1, float("nan"), float("inf"), True])
def test_cache_ttl_validation(invalid):
    with pytest.raises(ValueError):
        UserCache(ttl_seconds=invalid)
    with pytest.raises(ValueError):
        CachedSocialGraph(InMemorySocialGraph(), ttl_seconds=invalid)
