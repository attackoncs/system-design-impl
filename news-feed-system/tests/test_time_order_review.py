"""Aware timestamps retain instant ordering across a repeated wall-clock hour."""

import datetime as dt

import pytest
from news_feed_system import NewsFeedAPI
from news_feed_system.models import FeedEntry, Post
from news_feed_system.news_feed_cache import NewsFeedCache
from news_feed_system.storage import InMemoryPostStore, SQLitePostStore


class RepeatedHour(dt.tzinfo):
    """Model the fall-back hour without requiring an OS timezone database."""

    def utcoffset(self, value):
        return dt.timedelta(hours=-4 - value.fold)

    def dst(self, value):
        return dt.timedelta(0)


def posts():
    zone = RepeatedHour()
    early = dt.datetime(2026, 11, 1, 1, 50, tzinfo=zone, fold=0)
    late = dt.datetime(2026, 11, 1, 1, 10, tzinfo=zone, fold=1)
    return Post("early", "author", "early", created_at=early), Post(
        "late", "author", "late", created_at=late
    )


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_source_order_and_cursor_follow_instants_during_repeated_hour(
    backend, tmp_path
):
    store = (
        InMemoryPostStore()
        if backend == "memory"
        else SQLitePostStore(str(tmp_path / "feed.db"))
    )
    early, late = posts()
    store.put(early)
    store.put(late)
    assert [post.post_id for post in store.get_by_author("author")] == ["late", "early"]
    assert [
        post.post_id
        for post in store.get_by_author("author", before=(late.created_at, "late"))
    ] == ["early"]
    # Equal instants must still compare the ID, including a non-UTC boundary.
    store.put(Post("a", "author", "same instant", created_at=late.created_at))
    assert [
        post.post_id
        for post in store.get_by_author("author", before=(late.created_at, "late"))
    ] == ["a", "early"]


def test_feed_capacity_keeps_the_latest_instant_during_repeated_hour():
    cache = NewsFeedCache(max_entries=1)
    early, late = posts()
    for post in (late, early):
        cache.append("reader", FeedEntry(post.post_id, post.author_id, post.created_at))
    assert [entry.post_id for entry in cache.get_entries("reader")] == ["late"]
    assert cache.get_entries("reader")[0].timestamp is late.created_at


async def test_api_cursor_does_not_repeat_posts_during_repeated_hour():
    api = NewsFeedAPI()
    api.auth.register_token("token", "reader")
    await api.graph.add_follow("reader", "author")
    for post in posts():
        api.posts.put(post)
    first = await api.retrieve("token", page_size=1)
    assert [entry.post.post_id for entry in first.entries] == ["late"]
    assert first.has_more and first.next_cursor
    second = await api.retrieve("token", page_size=1, cursor=first.next_cursor)
    assert [entry.post.post_id for entry in second.entries] == ["early"]
    assert not second.has_more
