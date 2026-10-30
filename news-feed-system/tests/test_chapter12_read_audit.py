"""Read stages overlap within a budget and retain ownership of blocking I/O."""

import asyncio
import datetime as dt
import threading

import pytest

from news_feed_system.api import NewsFeedAPI
from news_feed_system.config import NewsFeedConfig
from news_feed_system.models import FeedEntry, Post, UserProfile


class BlockingRead:
    """Pause synchronous dependencies at a deterministic concurrency barrier."""

    def __init__(self, read, width=3, fail_key=None):
        self.read = read
        self.width = width
        self.fail_key = fail_key
        self.full = threading.Event()
        self.release = threading.Event()
        self.failed = threading.Event()
        self.lock = threading.Lock()
        self.active = 0
        self.maximum = 0
        self.calls = []

    def __call__(self, key):
        with self.lock:
            self.calls.append(key)
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            if self.active == self.width:
                self.full.set()
        try:
            if key == self.fail_key:
                assert self.full.wait(timeout=3), "sibling reads never overlapped"
                self.failed.set()
                raise OSError("profile or content source unavailable")
            assert self.release.wait(timeout=5), "read was not released"
            return self.read(key)
        finally:
            with self.lock:
                self.active -= 1


def distinct_authors(count):
    # Profile coalescing uses bounded stripes; avoid accidental collisions in
    # tests that specifically require several independent source reads.
    authors, stripes = [], set()
    candidate = 0
    while len(authors) < count:
        author = f"author{candidate}"
        candidate += 1
        stripe = hash(author) % 64
        if stripe not in stripes:
            authors.append(author)
            stripes.add(stripe)
    return authors


async def prepare_read(stage, monkeypatch, *, author_count=8, fail=False):
    api = NewsFeedAPI(config=NewsFeedConfig(read_concurrency=3))
    api.auth.register_token("reader-token", "reader")
    authors = distinct_authors(author_count)
    now = dt.datetime(2026, 10, 29, tzinfo=dt.timezone.utc)
    posts = []
    for index in range(8):
        author = authors[index % len(authors)]
        await api.graph.add_follow("reader", author)
        api.users.put(UserProfile(author, f"Profile {author}"))
        post = Post(
            f"post{index}",
            author,
            f"Content {index}",
            created_at=now + dt.timedelta(seconds=index),
        )
        api.posts.put(post)
        posts.append(post)
        if stage == "content":
            api.feeds.append("reader", FeedEntry(post.post_id, author, post.created_at))
    newest = posts[-1]
    if stage == "content":
        dependency, key = api.posts, newest.post_id
    else:
        dependency, key = api.users, newest.author_id
    read = BlockingRead(
        dependency.get, width=min(3, author_count), fail_key=key if fail else None
    )
    monkeypatch.setattr(dependency, "get", read)
    return api, read, posts


async def finish_read(request, read):
    read.release.set()
    if not request.done():
        request.cancel()
    await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("stage", ["content", "profiles"])
async def test_content_and_profile_reads_overlap_within_budget(stage, monkeypatch):
    api, read, posts = await prepare_read(stage, monkeypatch)
    request = asyncio.create_task(api.retrieve("reader-token", page_size=8))
    try:
        assert await asyncio.to_thread(read.full.wait, 2)
        assert read.maximum == 3
        read.release.set()
        feed = await asyncio.wait_for(request, 3)
        assert [entry.post.post_id for entry in feed.entries] == [
            post.post_id for post in reversed(posts)
        ]
        assert not feed.has_more
        assert len(read.calls) == 8
        assert read.maximum == 3
        assert read.active == 0
    finally:
        await finish_read(request, read)


@pytest.mark.parametrize("stage", ["content", "profiles"])
async def test_read_cancellation_drains_owned_blocking_calls(stage, monkeypatch):
    api, read, _ = await prepare_read(stage, monkeypatch)
    request = asyncio.create_task(api.retrieve("reader-token", page_size=8))
    try:
        assert await asyncio.to_thread(read.full.wait, 2)
        request.cancel()
        # Cancellation cannot report completion while its source I/O still runs.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(request), 0.05)
        read.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, 3)
        assert read.active == 0
        assert len(read.calls) == 3
    finally:
        await finish_read(request, read)


@pytest.mark.parametrize("stage", ["content", "profiles"])
async def test_read_failure_drains_siblings_before_returning(stage, monkeypatch):
    api, read, _ = await prepare_read(stage, monkeypatch, fail=True)
    request = asyncio.create_task(api.retrieve("reader-token", page_size=8))
    try:
        assert await asyncio.to_thread(read.failed.wait, 2)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(request), 0.05)
        read.release.set()
        with pytest.raises(OSError, match="source unavailable"):
            await asyncio.wait_for(request, 3)
        assert read.active == 0
        assert len(read.calls) == 3
    finally:
        await finish_read(request, read)


async def test_parallel_hydration_coalesces_repeated_author_misses(monkeypatch):
    api, read, _ = await prepare_read("profiles", monkeypatch, author_count=2)
    request = asyncio.create_task(api.retrieve("reader-token", page_size=8))
    try:
        assert await asyncio.to_thread(read.full.wait, 2)
        read.release.set()
        feed = await asyncio.wait_for(request, 3)
        assert len(feed.entries) == 8
        assert len(read.calls) == len(set(read.calls)) == 2
        assert read.maximum == 2
    finally:
        await finish_read(request, read)


async def test_parallel_hydration_rechecks_follow_and_mute_policy(monkeypatch):
    api, read, posts = await prepare_read("profiles", monkeypatch)
    request = asyncio.create_task(api.retrieve("reader-token", page_size=8))
    try:
        assert await asyncio.to_thread(read.full.wait, 2)
        await api.graph.remove_follow("reader", posts[-1].author_id)
        api.fanout.add_mute("reader", posts[-2].author_id)
        read.release.set()
        feed = await asyncio.wait_for(request, 3)
        assert [entry.post.post_id for entry in feed.entries] == [
            post.post_id for post in reversed(posts[:-2])
        ]
    finally:
        await finish_read(request, read)
