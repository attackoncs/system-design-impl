"""Notification service for alerting followers of new posts.

When a post is successfully persisted, the NotificationService records
a notification for each of the author's followers so they can be informed
that new content is available.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

from news_feed_system.social_graph_cache import CachedSocialGraph


class NotificationService:
    """Notifies followers when new content is published.

    Maintains an in-memory store of pending notifications per user.
    Notifications are recorded when an author publishes a post and can
    be retrieved by any follower to discover new content.
    """

    def __init__(self, social_graph: CachedSocialGraph) -> None:
        """Initialise the notification service.

        Args:
            social_graph: A CachedSocialGraph used to look up the
                followers of a post's author.
        """
        self._social_graph = social_graph
        # user_id -> list of (author_id, post_id) notification tuples
        self._notifications: Dict[str, List[tuple]] = defaultdict(list)

    async def notify_followers(self, author_id: str, post_id: str) -> None:
        """Record a notification for every follower of *author_id*.

        Fetches the author's follower list from the social graph cache and
        appends a ``(author_id, post_id)`` notification entry to each
        follower's pending notification queue.

        Args:
            author_id: The user who published the new post.
            post_id:   The unique identifier of the new post.
        """
        followers = await self._social_graph.get_followers(author_id)
        for follower_id in followers:
            self._notifications[follower_id].append((author_id, post_id))

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
        return list(self._notifications.get(user_id, []))
