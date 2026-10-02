"""Two-tier post cache with hot and normal tiers.

Provides a content cache for posts with automatic promotion and demotion
between hot (popular/recent) and normal tiers based on access patterns.
New posts are placed in the hot cache. When the hot cache reaches capacity,
the least recently used entries are demoted to the normal cache. Posts in
the normal cache that are accessed frequently are promoted to the hot cache.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Optional

from news_feed_system.models import Post


class PostCache:
    """Two-tier LRU cache for posts with hot/normal tier management.

    The hot cache holds popular and recently published posts. The normal
    cache holds less frequently accessed posts. Posts are promoted from
    normal to hot when their access count exceeds the configured threshold,
    and demoted from hot to normal via LRU eviction when the hot cache
    reaches capacity.

    Args:
        hot_capacity: Maximum number of posts in the hot cache tier.
        normal_capacity: Maximum number of posts in the normal cache tier.
        access_threshold: Number of accesses required to promote a post
            from normal to hot tier.
    """

    def __init__(
        self,
        hot_capacity: int = 1000,
        normal_capacity: int = 10000,
        access_threshold: int = 5,
    ) -> None:
        if hot_capacity <= 0:
            raise ValueError(f"hot_capacity must be positive, got {hot_capacity}")
        if normal_capacity <= 0:
            raise ValueError(f"normal_capacity must be positive, got {normal_capacity}")
        if access_threshold <= 0:
            raise ValueError(f"access_threshold must be positive, got {access_threshold}")

        self._hot_capacity = hot_capacity
        self._normal_capacity = normal_capacity
        self._access_threshold = access_threshold

        # OrderedDict for LRU: most recently used at the end
        self._hot: OrderedDict[str, Post] = OrderedDict()
        self._normal: OrderedDict[str, Post] = OrderedDict()

        # Track access counts for posts in the normal tier
        self._access_counts: dict[str, int] = {}

    @property
    def hot_capacity(self) -> int:
        """Maximum capacity of the hot cache tier."""
        return self._hot_capacity

    @property
    def normal_capacity(self) -> int:
        """Maximum capacity of the normal cache tier."""
        return self._normal_capacity

    @property
    def access_threshold(self) -> int:
        """Access count threshold for promotion from normal to hot."""
        return self._access_threshold

    @property
    def hot_size(self) -> int:
        """Current number of posts in the hot tier."""
        return len(self._hot)

    @property
    def normal_size(self) -> int:
        """Current number of posts in the normal tier."""
        return len(self._normal)

    def put(self, post: Post) -> None:
        """Add a new post to the cache.

        New posts are always placed in the hot cache. If the post already
        exists in either tier, it is moved to the hot cache. When the hot
        cache is at capacity, the least recently used entry is demoted to
        the normal cache.

        Args:
            post: The Post to cache.
        """
        post_id = post.post_id

        # Remove from normal if it exists there
        if post_id in self._normal:
            del self._normal[post_id]
            self._access_counts.pop(post_id, None)

        # If already in hot, just update and move to end (most recent)
        if post_id in self._hot:
            self._hot.move_to_end(post_id)
            self._hot[post_id] = post
            return

        # Need to add to hot; check if we need to evict
        if len(self._hot) >= self._hot_capacity:
            self._demote_lru_from_hot()

        self._hot[post_id] = post

    def get(self, post_id: str) -> Optional[Post]:
        """Retrieve a post by post_id.

        Checks the hot cache first, then the normal cache. Accessing a
        post in the hot cache marks it as most recently used. Accessing a
        post in the normal cache increments its access count and may
        trigger promotion to the hot cache.

        Args:
            post_id: The ID of the post to retrieve.

        Returns:
            The cached Post, or None if not found in either tier.
        """
        # Check hot cache first
        if post_id in self._hot:
            self._hot.move_to_end(post_id)
            return self._hot[post_id]

        # Check normal cache
        if post_id in self._normal:
            self._normal.move_to_end(post_id)

            # Increment access count
            count = self._access_counts.get(post_id, 0) + 1
            self._access_counts[post_id] = count

            # Check if promotion threshold is met
            if count >= self._access_threshold:
                return self._promote_to_hot(post_id)

            return self._normal[post_id]

        # Cache miss
        return None

    def _demote_lru_from_hot(self) -> None:
        """Demote the least recently used entry from hot to normal cache."""
        if not self._hot:
            return

        # Pop the LRU item (first item in OrderedDict)
        post_id, post = self._hot.popitem(last=False)

        # Add to normal cache; evict LRU from normal if at capacity
        if len(self._normal) >= self._normal_capacity:
            evicted_id, _ = self._normal.popitem(last=False)
            self._access_counts.pop(evicted_id, None)

        self._normal[post_id] = post
        # Reset access count for demoted post
        self._access_counts[post_id] = 0

    def _promote_to_hot(self, post_id: str) -> Post:
        """Promote a post from normal to hot cache.

        Args:
            post_id: The ID of the post to promote.

        Returns:
            The promoted Post.
        """
        post = self._normal.pop(post_id)
        self._access_counts.pop(post_id, None)

        # Make room in hot if needed
        if len(self._hot) >= self._hot_capacity:
            self._demote_lru_from_hot()

        self._hot[post_id] = post
        return post

    def __len__(self) -> int:
        """Return total number of posts across both tiers."""
        return len(self._hot) + len(self._normal)

    def contains(self, post_id: str) -> bool:
        """Check if a post exists in either tier.

        Args:
            post_id: The ID of the post to check.

        Returns:
            True if the post is in either the hot or normal cache.
        """
        return post_id in self._hot or post_id in self._normal

    def is_hot(self, post_id: str) -> bool:
        """Check if a post is in the hot tier.

        Args:
            post_id: The ID of the post to check.

        Returns:
            True if the post is in the hot cache tier.
        """
        return post_id in self._hot

    def is_normal(self, post_id: str) -> bool:
        """Check if a post is in the normal tier.

        Args:
            post_id: The ID of the post to check.

        Returns:
            True if the post is in the normal cache tier.
        """
        return post_id in self._normal
