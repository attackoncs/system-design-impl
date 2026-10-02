"""Unit tests for PostService.

Tests cover:
- Successful publish returns a unique post_id and persists to PostCache
- Empty content raises ValidationError
- Whitespace-only content raises ValidationError
- Invalid auth token raises AuthenticationError
- Rate limit exceeded raises RateLimitError
- FanoutService.fanout() is triggered after persistence
- NotificationService.notify_followers() is triggered after persistence
- NotificationService is optional (None is accepted)

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 13.1, 13.2, 13.3
"""

import asyncio
import sys
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

# AsyncMock was added in Python 3.8; provide a simple shim for 3.7
if sys.version_info >= (3, 8):
    from unittest.mock import AsyncMock
else:
    class AsyncMock(MagicMock):
        """Minimal AsyncMock shim for Python < 3.8."""
        async def __call__(self, *args, **kwargs):
            return super().__call__(*args, **kwargs)

from news_feed_system.auth import InMemoryAuthenticator
from news_feed_system.config import FanoutConfig, RateLimitConfig
from news_feed_system.exceptions import (
    AuthenticationError,
    RateLimitError,
    ValidationError,
)
from news_feed_system.fanout_service import FanoutService
from news_feed_system.models import FeedRequest, Post
from news_feed_system.post_cache import PostCache
from news_feed_system.post_service import PostService
from news_feed_system.queue import MessageQueue
from news_feed_system.rate_limiter import SlidingWindowRateLimiter
from news_feed_system.social_graph import InMemorySocialGraph
from news_feed_system.social_graph_cache import CachedSocialGraph


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_service(
    max_posts: int = 10,
    window_seconds: float = 3600.0,
    notification_service=None,
):
    """Build a PostService with in-memory collaborators."""
    auth = InMemoryAuthenticator()
    auth.register_token("valid-token", "user-1")

    rate_limiter = SlidingWindowRateLimiter(
        RateLimitConfig(max_posts=max_posts, window_seconds=window_seconds)
    )

    post_cache = PostCache(hot_capacity=100, normal_capacity=1000)

    social_graph = InMemorySocialGraph()
    cached_graph = CachedSocialGraph(social_graph)
    queue = MessageQueue(max_size=1000)
    fanout_service = FanoutService(
        social_graph=cached_graph,
        post_cache=post_cache,
        queue=queue,
        config=FanoutConfig(celebrity_threshold=5000),
    )

    service = PostService(
        authenticator=auth,
        rate_limiter=rate_limiter,
        post_cache=post_cache,
        fanout_service=fanout_service,
        notification_service=notification_service,
    )
    return service, auth, post_cache, fanout_service


# ---------------------------------------------------------------------------
# Requirement 1.2: successful publish returns a unique post_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_returns_post_id():
    """Successful publish returns a non-empty string post_id."""
    service, _, _, _ = _make_service()
    request = FeedRequest(user_id="user-1", auth_token="valid-token", content="Hello!")
    post_id = await service.publish(request)
    assert isinstance(post_id, str)
    assert len(post_id) > 0


@pytest.mark.asyncio
async def test_publish_returns_unique_post_ids():
    """Each publish call returns a distinct post_id."""
    service, _, _, _ = _make_service()
    request = FeedRequest(user_id="user-1", auth_token="valid-token", content="Hello!")
    id1 = await service.publish(request)
    id2 = await service.publish(request)
    assert id1 != id2


# ---------------------------------------------------------------------------
# Requirement 1.1: post is persisted to PostCache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_persists_post_to_cache():
    """After publish, the post is retrievable from PostCache."""
    service, _, post_cache, _ = _make_service()
    request = FeedRequest(user_id="user-1", auth_token="valid-token", content="Cached!")
    post_id = await service.publish(request)
    cached_post = post_cache.get(post_id)
    assert cached_post is not None
    assert cached_post.post_id == post_id
    assert cached_post.content == "Cached!"
    assert cached_post.author_id == "user-1"


# ---------------------------------------------------------------------------
# Requirement 1.5: empty content raises ValidationError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_empty_content_raises_validation_error():
    """Empty content string raises ValidationError."""
    service, _, _, _ = _make_service()
    request = FeedRequest(user_id="user-1", auth_token="valid-token", content="")
    with pytest.raises(ValidationError):
        await service.publish(request)


@pytest.mark.asyncio
async def test_publish_whitespace_content_raises_validation_error():
    """Whitespace-only content raises ValidationError."""
    service, _, _, _ = _make_service()
    request = FeedRequest(user_id="user-1", auth_token="valid-token", content="   ")
    with pytest.raises(ValidationError):
        await service.publish(request)


# ---------------------------------------------------------------------------
# Requirement 1.6 / 13.3: invalid auth token raises AuthenticationError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_invalid_token_raises_authentication_error():
    """An unregistered token raises AuthenticationError."""
    service, _, _, _ = _make_service()
    request = FeedRequest(
        user_id="user-1", auth_token="bad-token", content="Hello!"
    )
    with pytest.raises(AuthenticationError):
        await service.publish(request)


@pytest.mark.asyncio
async def test_publish_revoked_token_raises_authentication_error():
    """A revoked token raises AuthenticationError."""
    service, auth, _, _ = _make_service()
    auth.revoke_token("valid-token")
    request = FeedRequest(user_id="user-1", auth_token="valid-token", content="Hello!")
    with pytest.raises(AuthenticationError):
        await service.publish(request)


# ---------------------------------------------------------------------------
# Requirements 13.1, 13.2: rate limit exceeded raises RateLimitError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_rate_limit_exceeded_raises_rate_limit_error():
    """Exceeding the rate limit raises RateLimitError."""
    service, _, _, _ = _make_service(max_posts=2, window_seconds=3600.0)
    request = FeedRequest(user_id="user-1", auth_token="valid-token", content="Post!")

    await service.publish(request)
    await service.publish(request)

    with pytest.raises(RateLimitError) as exc_info:
        await service.publish(request)

    err = exc_info.value
    assert err.user_id == "user-1"
    assert err.limit == 2


# ---------------------------------------------------------------------------
# Requirement 1.3: FanoutService.fanout() is triggered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_triggers_fanout():
    """FanoutService.fanout() is called after successful persistence."""
    service, _, _, fanout_service = _make_service()
    fanout_service.fanout = AsyncMock()

    request = FeedRequest(user_id="user-1", auth_token="valid-token", content="Fanout!")
    post_id = await service.publish(request)

    fanout_service.fanout.assert_called_once()
    called_post: Post = fanout_service.fanout.call_args[0][0]
    assert called_post.post_id == post_id
    assert called_post.author_id == "user-1"


# ---------------------------------------------------------------------------
# Requirement 1.4: NotificationService.notify_followers() is triggered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_triggers_notification_service():
    """notify_followers() is called when NotificationService is provided."""
    mock_notifier = MagicMock()
    mock_notifier.notify_followers = AsyncMock()

    service, _, _, _ = _make_service(notification_service=mock_notifier)
    request = FeedRequest(user_id="user-1", auth_token="valid-token", content="Notify!")
    post_id = await service.publish(request)

    mock_notifier.notify_followers.assert_called_once_with("user-1", post_id)


@pytest.mark.asyncio
async def test_publish_without_notification_service_succeeds():
    """PostService works correctly when notification_service is None."""
    service, _, _, _ = _make_service(notification_service=None)
    request = FeedRequest(user_id="user-1", auth_token="valid-token", content="No notif!")
    post_id = await service.publish(request)
    assert post_id is not None


# ---------------------------------------------------------------------------
# Validation happens before auth (order of operations)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_checked_before_auth():
    """ValidationError is raised even when the token is invalid."""
    service, _, _, _ = _make_service()
    request = FeedRequest(user_id="user-1", auth_token="bad-token", content="")
    # Should raise ValidationError, not AuthenticationError
    with pytest.raises(ValidationError):
        await service.publish(request)


# ---------------------------------------------------------------------------
# Post content and metadata are preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_preserves_media_urls():
    """Media URLs from the request are stored in the persisted post."""
    service, _, post_cache, _ = _make_service()
    request = FeedRequest(
        user_id="user-1",
        auth_token="valid-token",
        content="With media",
        media_urls=["https://example.com/img.png"],
    )
    post_id = await service.publish(request)
    cached_post = post_cache.get(post_id)
    assert cached_post is not None
    assert cached_post.media_urls == ["https://example.com/img.png"]


@pytest.mark.asyncio
async def test_publish_preserves_selective_sharing():
    """Selective sharing list from the request is stored in the persisted post."""
    service, _, post_cache, _ = _make_service()
    request = FeedRequest(
        user_id="user-1",
        auth_token="valid-token",
        content="Selective",
        selective_sharing=["friend-1", "friend-2"],
    )
    post_id = await service.publish(request)
    cached_post = post_cache.get(post_id)
    assert cached_post is not None
    assert cached_post.selective_sharing == ["friend-1", "friend-2"]
