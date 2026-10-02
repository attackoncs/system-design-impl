"""Fanout service implementing hybrid push/pull distribution model.

Distributes new posts to followers' news feed caches using a hybrid strategy:
- Normal users (follower_count < celebrity_threshold): push fanout on write,
  batching followers into FanoutTask items enqueued to the MessageQueue.
- Celebrity users (follower_count >= celebrity_threshold): store post in
  PostCache only; followers pull celebrity posts at read time.

Feed filtering is applied before fanout:
- Mute list: followers who have muted the author are excluded.
- Selective sharing: if the post has a selective_sharing list, only followers
  in that list receive the post.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, List, Optional, Set

from news_feed_system.config import FanoutConfig
from news_feed_system.models import FanoutTask, Post
from news_feed_system.post_cache import PostCache
from news_feed_system.queue import MessageQueue
from news_feed_system.social_graph_cache import CachedSocialGraph

logger = logging.getLogger(__name__)


class FanoutService:
    """Hybrid push/pull fanout service for news feed distribution.

    Classifies authors as celebrities based on follower count and applies
    the appropriate fanout strategy. Supports per-user mute lists and
    per-post selective sharing filters.

    Args:
        social_graph: Cached social graph for follower lookups.
        post_cache: Post cache for storing celebrity posts.
        queue: Message queue for enqueuing fanout tasks.
        config: Fanout configuration (celebrity_threshold, batch_size, etc.).
    """

    def __init__(
        self,
        social_graph: CachedSocialGraph,
        post_cache: PostCache,
        queue: MessageQueue,
        config: Optional[FanoutConfig] = None,
    ) -> None:
        self._social_graph = social_graph
        self._post_cache = post_cache
        self._queue = queue
        self._config = config or FanoutConfig()

        # user_id -> set of muted user_ids
        # mute_lists[follower_id] = {author_id, ...}
        self._mute_lists: Dict[str, Set[str]] = defaultdict(set)

    @property
    def celebrity_threshold(self) -> int:
        """The follower count at or above which a user is a celebrity."""
        return self._config.celebrity_threshold

    # ------------------------------------------------------------------
    # Mute list management (Requirement 12.1, 12.5)
    # ------------------------------------------------------------------

    def add_mute(self, user_id: str, muted_id: str) -> None:
        """Add a user to the mute list of another user.

        After this call, posts from ``muted_id`` will not be fanned out
        to ``user_id``.

        Args:
            user_id: The user who wants to mute someone.
            muted_id: The user to be muted.
        """
        self._mute_lists[user_id].add(muted_id)
        logger.debug("User %s muted %s", user_id, muted_id)

    def remove_mute(self, user_id: str, muted_id: str) -> None:
        """Remove a user from the mute list of another user.

        Args:
            user_id: The user who wants to unmute someone.
            muted_id: The user to be unmuted.
        """
        self._mute_lists[user_id].discard(muted_id)
        logger.debug("User %s unmuted %s", user_id, muted_id)

    def get_mute_list(self, user_id: str) -> Set[str]:
        """Return the set of user IDs muted by ``user_id``.

        Args:
            user_id: The user whose mute list to retrieve.

        Returns:
            A set of muted user IDs (may be empty).
        """
        return set(self._mute_lists[user_id])

    # ------------------------------------------------------------------
    # Celebrity classification (Requirement 3.1, 3.2, 3.6)
    # ------------------------------------------------------------------

    async def is_celebrity(self, user_id: str) -> bool:
        """Return True if the user's follower count meets the celebrity threshold.

        Requirement 3.1: classify users as celebrities when
        follower_count >= celebrity_threshold.

        Args:
            user_id: The user to classify.

        Returns:
            True if the user is a celebrity, False otherwise.
        """
        follower_count = await self._social_graph.get_follower_count(user_id)
        return follower_count >= self._config.celebrity_threshold

    # ------------------------------------------------------------------
    # Core fanout logic (Requirements 3.3, 3.4, 4.1–4.4)
    # ------------------------------------------------------------------

    async def fanout(self, post: Post) -> None:
        """Distribute a post to followers using the hybrid push/pull model.

        For non-celebrity authors (Requirement 3.3):
          1. Fetch followers from the CachedSocialGraph (Req 4.1).
          2. Filter out muted followers (Req 4.3, 12.3).
          3. Filter by selective sharing list if set (Req 4.3, 12.4).
          4. Batch filtered followers into FanoutTask items (Req 4.4).
          5. Enqueue each batch to the MessageQueue (Req 4.4).

        For celebrity authors (Requirement 3.4):
          - Store the post in PostCache only; no push to followers.

        Args:
            post: The newly published Post to distribute.
        """
        celebrity = await self.is_celebrity(post.author_id)

        if celebrity:
            # Celebrity fanout-on-read: store in PostCache, skip push
            self._post_cache.put(post)
            logger.debug(
                "Celebrity fanout: stored post %s for author %s in PostCache",
                post.post_id,
                post.author_id,
            )
            return

        # Non-celebrity fanout-on-write
        followers = await self._social_graph.get_followers(post.author_id)

        # Apply filtering
        filtered = self._filter_followers(
            followers=followers,
            author_id=post.author_id,
            selective_sharing=post.selective_sharing,
        )

        if not filtered:
            logger.debug(
                "No eligible followers for post %s after filtering", post.post_id
            )
            return

        # Batch and enqueue
        await self._enqueue_batches(post, filtered)

    def _filter_followers(
        self,
        followers: List[str],
        author_id: str,
        selective_sharing: Optional[List[str]],
    ) -> List[str]:
        """Apply mute and selective sharing filters to a follower list.

        Requirement 12.3: exclude followers who have muted the author.
        Requirement 12.4: if selective_sharing is set, exclude followers
            not in the list.

        Args:
            followers: Full list of follower user IDs.
            author_id: The post author's user ID.
            selective_sharing: Optional allowlist of follower IDs. When
                None, the post is public to all followers.

        Returns:
            Filtered list of follower IDs eligible to receive the post.
        """
        # Build selective sharing set for O(1) lookup
        sharing_set: Optional[Set[str]] = (
            set(selective_sharing) if selective_sharing is not None else None
        )

        result: List[str] = []
        for follower_id in followers:
            # Mute filter: skip followers who have muted the author
            if author_id in self._mute_lists.get(follower_id, set()):
                continue

            # Selective sharing filter: skip followers not in the allowlist
            if sharing_set is not None and follower_id not in sharing_set:
                continue

            result.append(follower_id)

        return result

    async def _enqueue_batches(self, post: Post, followers: List[str]) -> None:
        """Split followers into batches and enqueue FanoutTask items.

        Requirement 4.4: create FanoutTask items and enqueue to MessageQueue.

        Args:
            post: The post being distributed.
            followers: Filtered list of follower IDs to receive the post.
        """
        batch_size = self._config.batch_size
        for i in range(0, len(followers), batch_size):
            batch = followers[i : i + batch_size]
            task = FanoutTask(
                post_id=post.post_id,
                author_id=post.author_id,
                target_user_ids=batch,
                timestamp=post.created_at,
            )
            await self._queue.enqueue(task)
            logger.debug(
                "Enqueued FanoutTask for post %s with %d followers (batch %d)",
                post.post_id,
                len(batch),
                i // batch_size,
            )
