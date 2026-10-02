"""Post service for feed publishing.

Handles the full feed publishing pipeline:
  1. Validate content is non-empty (raises ValidationError)
  2. Authenticate the auth_token (raises AuthenticationError)
  3. Rate-limit the user (raises RateLimitError)
  4. Persist the post to PostCache
  5. Trigger FanoutService.fanout() asynchronously
  6. Trigger NotificationService.notify_followers() if available

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 13.1, 13.2, 13.3
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Optional

from news_feed_system.auth import Authenticator
from news_feed_system.exceptions import AuthenticationError, ValidationError
from news_feed_system.fanout_service import FanoutService
from news_feed_system.models import FeedRequest, Post
from news_feed_system.post_cache import PostCache
from news_feed_system.rate_limiter import SlidingWindowRateLimiter

if TYPE_CHECKING:
    from news_feed_system.notification_service import NotificationService

logger = logging.getLogger(__name__)


class PostService:
    """Service responsible for persisting posts and triggering fanout.

    Orchestrates the full feed publishing pipeline: validation,
    authentication, rate limiting, persistence, fanout, and notification.

    Args:
        authenticator: Token validator used to authenticate requests.
        rate_limiter: Sliding-window rate limiter for post publishing.
        post_cache: Two-tier content cache where posts are persisted.
        fanout_service: Service that distributes posts to followers.
        notification_service: Optional service that notifies followers.
            When None, notification is skipped silently.
    """

    def __init__(
        self,
        authenticator: Authenticator,
        rate_limiter: SlidingWindowRateLimiter,
        post_cache: PostCache,
        fanout_service: FanoutService,
        notification_service: Optional["NotificationService"] = None,
    ) -> None:
        self._authenticator = authenticator
        self._rate_limiter = rate_limiter
        self._post_cache = post_cache
        self._fanout_service = fanout_service
        self._notification_service = notification_service

    async def publish(self, request: FeedRequest) -> str:
        """Publish a post from a feed request.

        Executes the full publishing pipeline:
          1. Validate that content is non-empty (Requirement 1.5).
          2. Authenticate the auth_token (Requirement 1.6, 13.3).
          3. Enforce rate limit for the user (Requirements 13.1, 13.2).
          4. Persist the post to PostCache (Requirement 1.1).
          5. Trigger fanout to followers (Requirement 1.3).
          6. Notify followers if NotificationService is available (Req 1.4).

        Args:
            request: The feed publish request containing auth_token,
                content, and optional media_urls / selective_sharing.

        Returns:
            A unique post_id (UUID string) for the newly created post
            (Requirement 1.2).

        Raises:
            ValidationError: If content is empty or whitespace-only
                (Requirement 1.5).
            AuthenticationError: If auth_token is invalid or missing
                (Requirement 1.6).
            RateLimitError: If the user has exceeded the publishing rate
                limit (Requirements 13.1, 13.2).
        """
        # --- Step 1: Validate content (Requirement 1.5) ---
        if not request.content or not request.content.strip():
            raise ValidationError(
                "Post content must not be empty", field="content"
            )

        # --- Step 2: Authenticate (Requirements 1.6, 13.3) ---
        user_id = await self._authenticator.validate_token(request.auth_token)
        if user_id is None:
            raise AuthenticationError()

        # --- Step 3: Rate limit (Requirements 13.1, 13.2) ---
        # check_and_record raises RateLimitError if limit exceeded
        self._rate_limiter.check_and_record(user_id)

        # --- Step 4: Persist post (Requirement 1.1) ---
        post_id = str(uuid.uuid4())
        post = Post(
            post_id=post_id,
            author_id=user_id,
            content=request.content,
            media_urls=list(request.media_urls),
            selective_sharing=(
                list(request.selective_sharing)
                if request.selective_sharing is not None
                else None
            ),
        )
        self._post_cache.put(post)
        logger.debug("Persisted post %s for user %s", post_id, user_id)

        # --- Step 5: Trigger fanout (Requirement 1.3) ---
        await self._fanout_service.fanout(post)
        logger.debug("Fanout triggered for post %s", post_id)

        # --- Step 6: Notify followers (Requirement 1.4) ---
        if self._notification_service is not None:
            await self._notification_service.notify_followers(user_id, post_id)
            logger.debug("Notifications sent for post %s", post_id)

        # --- Return unique post_id (Requirement 1.2) ---
        return post_id
