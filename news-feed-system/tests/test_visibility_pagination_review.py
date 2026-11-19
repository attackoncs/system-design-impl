"""Visibility changes cannot discard the continuation to older visible posts."""

import asyncio
import datetime as dt
import threading

import pytest
from news_feed_system import NewsFeedAPI, Post


@pytest.mark.asyncio
@pytest.mark.parametrize("change, hidden_count", [("unfollow", 3), ("mute", 2)])
async def test_visibility_change_preserves_continuation(
    monkeypatch, change, hidden_count
):
    api = NewsFeedAPI()
    api.auth.register_token("token", "reader")
    for author in ("hidden", "visible"):
        await api.graph.add_follow("reader", author)
    instant = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    for index in range(3 + hidden_count):
        api.posts.put(
            Post(
                str(index),
                "hidden" if index >= 3 else "visible",
                str(index),
                created_at=instant + dt.timedelta(seconds=index),
            )
        )
    started, release = threading.Event(), threading.Event()
    original = api.users.get

    def get_profile(author):
        if author == "hidden":
            started.set()
            if not release.wait(3):
                raise TimeoutError("profile read was not released")
        return original(author)

    monkeypatch.setattr(api.users, "get", get_profile)
    request = asyncio.create_task(api.retrieve("token", page_size=2))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        if change == "unfollow":
            await api.graph.remove_follow("reader", "hidden")
        else:
            api.fanout.add_mute("reader", "hidden")
        release.set()
        feed = await asyncio.wait_for(request, 3)
        seen = [entry.post.post_id for entry in feed.entries]
        assert len(seen) == 3 - hidden_count
        assert feed.has_more and feed.next_cursor
        for _ in range(2):
            feed = await api.retrieve("token", page_size=2, cursor=feed.next_cursor)
            seen.extend(entry.post.post_id for entry in feed.entries)
            if not feed.has_more:
                break
        assert seen == ["2", "1", "0"]
        assert not feed.has_more and feed.next_cursor is None
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
