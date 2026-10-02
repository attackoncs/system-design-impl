"""Unit tests for NotificationService."""

from __future__ import annotations

import pytest

from news_feed_system.notification_service import NotificationService
from news_feed_system.social_graph import InMemorySocialGraph
from news_feed_system.social_graph_cache import CachedSocialGraph


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_service() -> tuple[NotificationService, CachedSocialGraph, InMemorySocialGraph]:
    """Return a fresh (NotificationService, CachedSocialGraph, InMemorySocialGraph) triple."""
    graph = InMemorySocialGraph()
    cached = CachedSocialGraph(backend=graph)
    service = NotificationService(social_graph=cached)
    return service, cached, graph


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_notifications_initially():
    """A user with no notifications gets an empty list."""
    service, _, _ = make_service()
    assert service.get_notifications("user_1") == []


@pytest.mark.asyncio
async def test_notify_single_follower():
    """A follower receives a notification when the author publishes."""
    service, cached, _ = make_service()
    await cached.add_follow("follower_1", "author_1")

    await service.notify_followers("author_1", "post_abc")

    notifications = service.get_notifications("follower_1")
    assert len(notifications) == 1
    assert notifications[0] == ("author_1", "post_abc")


@pytest.mark.asyncio
async def test_notify_multiple_followers():
    """All followers receive a notification."""
    service, cached, _ = make_service()
    await cached.add_follow("follower_1", "author_1")
    await cached.add_follow("follower_2", "author_1")
    await cached.add_follow("follower_3", "author_1")

    await service.notify_followers("author_1", "post_xyz")

    for follower in ("follower_1", "follower_2", "follower_3"):
        notifs = service.get_notifications(follower)
        assert len(notifs) == 1
        assert notifs[0] == ("author_1", "post_xyz")


@pytest.mark.asyncio
async def test_non_follower_receives_no_notification():
    """A user who does not follow the author gets no notification."""
    service, cached, _ = make_service()
    await cached.add_follow("follower_1", "author_1")

    await service.notify_followers("author_1", "post_abc")

    assert service.get_notifications("non_follower") == []


@pytest.mark.asyncio
async def test_no_followers_no_notifications():
    """Notifying for an author with zero followers produces no notifications."""
    service, _, _ = make_service()
    await service.notify_followers("lonely_author", "post_1")
    # No exception; no notifications recorded for anyone
    assert service.get_notifications("anyone") == []


@pytest.mark.asyncio
async def test_multiple_posts_accumulate_notifications():
    """Multiple posts from the same author accumulate in follower's list."""
    service, cached, _ = make_service()
    await cached.add_follow("follower_1", "author_1")

    await service.notify_followers("author_1", "post_1")
    await service.notify_followers("author_1", "post_2")
    await service.notify_followers("author_1", "post_3")

    notifications = service.get_notifications("follower_1")
    assert len(notifications) == 3
    assert notifications[0] == ("author_1", "post_1")
    assert notifications[1] == ("author_1", "post_2")
    assert notifications[2] == ("author_1", "post_3")


@pytest.mark.asyncio
async def test_notifications_from_multiple_authors():
    """A follower who follows several authors receives notifications from all."""
    service, cached, _ = make_service()
    await cached.add_follow("follower_1", "author_a")
    await cached.add_follow("follower_1", "author_b")

    await service.notify_followers("author_a", "post_a1")
    await service.notify_followers("author_b", "post_b1")

    notifications = service.get_notifications("follower_1")
    assert len(notifications) == 2
    assert ("author_a", "post_a1") in notifications
    assert ("author_b", "post_b1") in notifications


@pytest.mark.asyncio
async def test_get_notifications_returns_copy():
    """get_notifications returns a copy; mutating it does not affect the store."""
    service, cached, _ = make_service()
    await cached.add_follow("follower_1", "author_1")
    await service.notify_followers("author_1", "post_1")

    first_call = service.get_notifications("follower_1")
    first_call.clear()  # mutate the returned list

    second_call = service.get_notifications("follower_1")
    assert len(second_call) == 1  # original store is unaffected


@pytest.mark.asyncio
async def test_removed_follower_does_not_receive_notification():
    """After unfollowing, the user no longer receives notifications."""
    service, cached, _ = make_service()
    await cached.add_follow("follower_1", "author_1")
    await cached.remove_follow("follower_1", "author_1")

    await service.notify_followers("author_1", "post_1")

    assert service.get_notifications("follower_1") == []
