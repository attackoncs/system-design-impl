"""A complete feed page must not depend on every retained timeline ID."""

import datetime as dt
import threading

import pytest

from news_feed_system import NewsFeedAPI
from news_feed_system.config import NewsFeedConfig
from news_feed_system.models import FeedEntry, FeedRequest, Post, UserProfile
from news_feed_system.exceptions import ValidationError


async def prepared_feed(tmp_path, durable, private_count=0):
    api = NewsFeedAPI(
        NewsFeedConfig(read_concurrency=4, read_repair_seconds=3600),
        database_path=str(tmp_path / "feed.db") if durable else None,
    )
    api.auth.register_token("reader-token", "reader")
    api.users.put(UserProfile("author", "Author"))
    await api.graph.add_follow("reader", "author")
    posts = []
    start = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    for index in range(120):
        post = Post(
            f"post{index:03}",
            "author",
            f"Content {index}",
            created_at=start + dt.timedelta(seconds=index),
            selective_sharing=[] if index >= 120 - private_count else None,
        )
        api.posts.put(post)
        api.feeds.append(
            "reader", FeedEntry(post.post_id, post.author_id, post.created_at)
        )
        posts.append(post)
    # Establish a complete source window before testing the warm-cache path.
    await api.retrieve("reader-token", page_size=20)
    return api, posts


def counted_reads(api, monkeypatch, fail_id=None):
    original = api.posts.get
    calls, lock = [], threading.Lock()

    def read(post_id):
        with lock:
            calls.append(post_id)
        if post_id == fail_id:
            raise OSError("old content is unavailable")
        return original(post_id)

    monkeypatch.setattr(api.posts, "get", read)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("page_size", [1, 5, 20])
async def test_warm_page_reads_only_a_bounded_visible_window(
    tmp_path, monkeypatch, durable, page_size
):
    api, posts = await prepared_feed(tmp_path, durable)
    calls = counted_reads(api, monkeypatch)
    feed = await api.retrieve("reader-token", page_size=page_size)
    assert [entry.post.post_id for entry in feed.entries] == [
        post.post_id for post in reversed(posts[-page_size:])
    ]
    assert feed.has_more and feed.next_cursor
    assert len(calls) <= page_size + 1 + api.config.read_concurrency - 1


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_unneeded_old_content_failure_cannot_break_complete_page(
    tmp_path, monkeypatch, durable
):
    api, posts = await prepared_feed(tmp_path, durable)
    calls = counted_reads(api, monkeypatch, fail_id=posts[0].post_id)
    feed = await api.retrieve("reader-token", page_size=5)
    assert len(feed.entries) == 5 and feed.has_more
    assert posts[0].post_id not in calls


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_private_cached_prefix_is_skipped_until_page_and_sentinel_are_full(
    tmp_path, monkeypatch, durable
):
    api, posts = await prepared_feed(tmp_path, durable, private_count=25)
    calls = counted_reads(api, monkeypatch)
    feed = await api.retrieve("reader-token", page_size=5)
    visible = [post for post in reversed(posts) if post.selective_sharing is None]
    assert [entry.post.post_id for entry in feed.entries] == [
        post.post_id for post in visible[:5]
    ]
    assert feed.has_more and feed.next_cursor
    assert 25 + 6 <= len(calls) <= 25 + 6 + api.config.read_concurrency - 1


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize(
    "url",
    [
        "https://example.org:bad/image.png",
        "https://example.org:65536/image.png",
        "https://exa mple.org/image.png",
        "https://example.org/image\n.png",
        "https://example.org/image\\file.png",
        "https://example.org/\ud800.png",
    ],
)
async def test_invalid_media_urls_fail_validation_before_source_acceptance(
    tmp_path, durable, url
):
    api = NewsFeedAPI(database_path=str(tmp_path / "feed.db") if durable else None)
    api.auth.register_token("author-token", "author")
    with pytest.raises(ValidationError) as error:
        await api.publisher.publish(
            FeedRequest("author", "author-token", "Photo", [url])
        )
    assert error.value.field == "media_urls"
    assert api.post_store.pending_posts(100) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize(
    "url", ["https://例子.中国/图片.jpg", "http://[::1]:8080/photo.png"]
)
async def test_valid_international_and_ipv6_media_urls_are_preserved(
    tmp_path, durable, url
):
    api = NewsFeedAPI(database_path=str(tmp_path / "feed.db") if durable else None)
    api.auth.register_token("author-token", "author")
    post_id = await api.publisher.publish(
        FeedRequest("author", "author-token", "Photo", [url])
    )
    assert api.post_store.get(post_id).media_urls == [url]


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_unencodable_content_is_rejected_before_source_acceptance(
    tmp_path, durable
):
    api = NewsFeedAPI(database_path=str(tmp_path / "feed.db") if durable else None)
    api.auth.register_token("author-token", "author")
    with pytest.raises(ValidationError) as error:
        await api.publisher.publish(
            FeedRequest("author", "author-token", "Invalid\ud800")
        )
    assert error.value.field == "content"
    assert api.post_store.pending_posts(100) == []
