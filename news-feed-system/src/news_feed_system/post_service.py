"""Authenticated publication with a replayable fanout outbox."""

from __future__ import annotations

import asyncio
from news_feed_system.concurrency import run_sync
import dataclasses
import logging
import uuid
from typing import Optional
from urllib.parse import urlsplit

from news_feed_system.auth import Authenticator
from news_feed_system.exceptions import AuthenticationError, ValidationError
from news_feed_system.fanout_service import FanoutService
from news_feed_system.models import FeedRequest, Post
from news_feed_system.notification_service import NotificationService
from news_feed_system.post_cache import PostCache
from news_feed_system.rate_limiter import SlidingWindowRateLimiter

logger = logging.getLogger(__name__)


class PostService:
    def __init__(
        self,
        authenticator: Authenticator,
        rate_limiter: SlidingWindowRateLimiter,
        post_cache: PostCache,
        fanout_service: FanoutService,
        notification_service: Optional[NotificationService] = None,
        *,
        dispatch_on_publish: bool = True,
    ) -> None:
        self._authenticator = authenticator
        self._rate_limiter = rate_limiter
        self._post_cache = post_cache
        self._fanout_service = fanout_service
        self._notification_service = notification_service
        self._dispatch_lock = asyncio.Lock()
        self._dispatch_on_publish = dispatch_on_publish

    async def publish(self, request: FeedRequest) -> str:
        request = self._snapshot_request(request)
        if not isinstance(request.auth_token, str) or not request.auth_token:
            raise AuthenticationError()
        user_id = await self._authenticator.validate_token(request.auth_token)
        if user_id is None or (request.user_id and request.user_id != user_id):
            raise AuthenticationError()
        self._rate_limiter.check_and_record(user_id)
        post = Post(
            post_id=str(uuid.uuid4()),
            author_id=user_id,
            content=request.content,
            media_urls=request.media_urls,
            selective_sharing=request.selective_sharing,
        )
        # Source and pending outbox are committed atomically by cache.put.
        await run_sync(self._post_cache.put, post)
        if not self._dispatch_on_publish:
            return post.post_id
        try:
            await self._dispatch(post)
        except Exception:
            logger.exception("Post %s accepted; fanout pending", post.post_id)
        return post.post_id

    async def recover_pending(self, limit: int = 100) -> int:
        """Replay after graph/mutes are loaded. Failures retain pending rows."""
        pending = await run_sync(self._post_cache.store.pending_posts, limit)
        completed = 0
        for post in pending:
            try:
                completed += await self._dispatch(post)
            except Exception:
                logger.exception("Post %s fanout still pending", post.post_id)
        return completed

    async def _dispatch(self, post: Post) -> bool:
        async with self._dispatch_lock:
            if not await run_sync(self._post_cache.store.is_pending, post.post_id):
                return False
            try:
                await self._fanout_service.fanout(post)
                if self._notification_service is not None:
                    await self._notification_service.notify_followers(
                        post.author_id, post.post_id
                    )
                await run_sync(self._post_cache.store.mark_dispatched, post.post_id)
            except Exception:
                await run_sync(self._post_cache.store.defer, post.post_id)
                raise
            return True

    @staticmethod
    def _snapshot_request(request: FeedRequest) -> FeedRequest:
        if not isinstance(request.content, str) or not request.content.strip():
            raise ValidationError("Post content must not be empty", field="content")
        if len(request.content) > 10000:
            raise ValidationError(
                "Post content exceeds 10000 characters", field="content"
            )
        for field in ("media_urls", "selective_sharing"):
            value = getattr(request, field)
            if value is None and field == "selective_sharing":
                continue
            if not isinstance(value, (list, tuple)) or any(
                not isinstance(item, str) or not item for item in value
            ):
                raise ValidationError(
                    f"{field} must contain nonempty strings", field=field
                )
        if len(request.media_urls) > 64:
            raise ValidationError(
                "At most 64 media URLs are accepted", field="media_urls"
            )
        if (
            request.selective_sharing is not None
            and len(request.selective_sharing) > 5000
        ):
            raise ValidationError(
                "At most 5000 recipients are accepted", field="selective_sharing"
            )
        for url in request.media_urls:
            try:
                parsed = urlsplit(url)
                valid = parsed.scheme in {"https", "http"} and bool(parsed.hostname)
            except ValueError:
                valid = False
            if len(url) > 2048 or not valid:
                raise ValidationError("Media must use HTTP(S) URLs", field="media_urls")
        return dataclasses.replace(
            request,
            media_urls=list(request.media_urls),
            selective_sharing=None
            if request.selective_sharing is None
            else list(request.selective_sharing),
        )
