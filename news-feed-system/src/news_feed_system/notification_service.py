"""Notification service for alerting followers of new posts.

When a post is successfully persisted, the NotificationService records a
notification for each of the author's followers so they can be informed that
new content is available.
"""

from __future__ import annotations

import collections
import threading
from typing import Dict, List

import news_feed_system.social_graph_cache
from news_feed_system.concurrency import gather_owned, run_sync


class NotificationService:
    """Notifies followers when new content is published.

    Maintains an in-memory store of pending notifications per user.
    Notifications are recorded when an author publishes a post and can be
    retrieved by any follower to discover new content.
    """

    def __init__(
        self,
        social_graph: news_feed_system.social_graph_cache.CachedSocialGraph,
        *,
        post_cache=None,
        visibility=None,
        max_pending: int = 1000,
        concurrency: int = 16,
    ) -> None:
        """Initialise the notification service.

        Args:
            social_graph: A CachedSocialGraph used to look up the
                followers of a post's author.
            concurrency: Maximum recipient relationship checks in flight.
                A fixed worker set avoids one asyncio task per follower.
        """
        self._social_graph = social_graph
        if (
            isinstance(max_pending, bool)
            or not isinstance(max_pending, int)
            or max_pending <= 0
        ):
            raise ValueError("max_pending must be a positive integer")
        self._post_cache = post_cache
        self._visibility = visibility
        self._max_pending = max_pending
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or concurrency <= 0
        ):
            raise ValueError("concurrency must be a positive integer")
        self._concurrency = concurrency
        self._lock = threading.RLock()
        # user_id -> list of (author_id, post_id) notification tuples
        self._notifications: Dict[str, List[tuple]] = collections.defaultdict(list)

    async def notify_followers(self, author_id: str, post_id: str) -> None:
        """Record a notification for every follower of *author_id*.

        Fetches the author's follower list from the social graph cache and
        appends a ``(author_id, post_id)`` notification entry to each
        follower's pending notification queue.

        Args:
            author_id: The user who published the new post.
            post_id:   The unique identifier of the new post.
        """
        followers = list(await self._social_graph.get_followers(author_id))
        post = None
        if self._post_cache is not None:
            post = await run_sync(self._post_cache.get, post_id)
            if post is None:
                return
        pending_followers = iter(followers)

        async def validate_and_record() -> None:
            for follower_id in pending_followers:
                # Relationship mutations may complete while the post is loaded
                # or another recipient is checked; validate immediately before
                # synchronous privacy checking and recording.
                if not await self._social_graph.is_following(follower_id, author_id):
                    continue
                with self._lock:
                    if (
                        post is not None
                        and self._visibility is not None
                        and not self._visibility(follower_id, post)
                    ):
                        continue
                    pending = self._notifications[follower_id]
                    entry = (author_id, post_id)
                    if entry not in pending:
                        pending.append(entry)
                        if len(pending) > self._max_pending:
                            del pending[0]

        # A fixed number of child tasks bounds concurrent database checks and
        # avoids allocating one asyncio task per follower of a large account.
        await gather_owned(
            *(
                validate_and_record()
                for _ in range(min(self._concurrency, len(followers)))
            )
        )

    def get_notifications(self, user_id: str) -> List[tuple]:
        """Return the list of pending notifications for *user_id*.

        Each notification is a ``(author_id, post_id)`` tuple indicating
        that *author_id* published *post_id*.  Notifications are returned
        in the order they were recorded (oldest first).

        Args:
            user_id: The user whose notifications should be retrieved.

        Returns:
            A list of ``(author_id, post_id)`` tuples.  Returns an empty
            list when there are no pending notifications.
        """
        with self._lock:
            return list(self._notifications.get(user_id, []))
