"""Caching layer over a SocialGraph backend.

Maintains separate TTL-based caches for follower and following lists.
Invalidates affected entries on relationship mutations.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional, Tuple

from news_feed_system.social_graph import SocialGraph


class CachedSocialGraph:
    """Caching layer over a SocialGraph backend.

    Maintains separate TTL-based caches for follower and following lists.
    Invalidates affected entries on relationship mutations.
    """

    def __init__(
        self,
        backend: SocialGraph,
        ttl_seconds: float = 300.0,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._backend = backend
        self._ttl = ttl_seconds
        self._clock = clock or time.monotonic
        # user_id -> (list[str], expiry_timestamp)
        self._follower_cache: Dict[str, Tuple[List[str], float]] = {}
        self._following_cache: Dict[str, Tuple[List[str], float]] = {}

    async def get_followers(self, user_id: str) -> list[str]:
        """Get followers with cache-through semantics."""
        cached = self._follower_cache.get(user_id)
        if cached and self._clock() < cached[1]:
            return cached[0]
        result = await self._backend.get_followers(user_id)
        self._follower_cache[user_id] = (result, self._clock() + self._ttl)
        return result

    async def get_following(self, user_id: str) -> list[str]:
        """Get following with cache-through semantics."""
        cached = self._following_cache.get(user_id)
        if cached and self._clock() < cached[1]:
            return cached[0]
        result = await self._backend.get_following(user_id)
        self._following_cache[user_id] = (result, self._clock() + self._ttl)
        return result

    async def get_follower_count(self, user_id: str) -> int:
        """Get follower count (delegates to backend)."""
        return await self._backend.get_follower_count(user_id)

    async def is_following(self, follower_id: str, followee_id: str) -> bool:
        """Check if a follower relationship exists (delegates to backend)."""
        return await self._backend.is_following(follower_id, followee_id)

    async def add_follow(self, follower_id: str, followee_id: str) -> None:
        """Add follow and invalidate affected caches."""
        await self._backend.add_follow(follower_id, followee_id)
        self._follower_cache.pop(followee_id, None)
        self._following_cache.pop(follower_id, None)

    async def remove_follow(self, follower_id: str, followee_id: str) -> None:
        """Remove follow and invalidate affected caches."""
        await self._backend.remove_follow(follower_id, followee_id)
        self._follower_cache.pop(followee_id, None)
        self._following_cache.pop(follower_id, None)

    def invalidate(self, user_id: str) -> None:
        """Manually invalidate all cached data for a user."""
        self._follower_cache.pop(user_id, None)
        self._following_cache.pop(user_id, None)
