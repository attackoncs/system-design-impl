"""User profile cache with LRU eviction and TTL-based expiration.

Provides fast access to user profile data during feed hydration and
fanout filtering, avoiding repeated lookups to the user store.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Callable, Dict, List, Optional, Tuple

from news_feed_system.models import UserProfile


class UserCache:
    """LRU cache for user profiles with configurable capacity and TTL.

    Entries are evicted when the cache exceeds capacity (least recently used
    first) or when their TTL expires. Supports single and batch retrieval.

    Args:
        capacity: Maximum number of entries the cache can hold.
        ttl_seconds: Time-to-live for each entry in seconds.
        clock: Callable returning the current time in seconds (default: time.monotonic).
    """

    def __init__(
        self,
        capacity: int = 5000,
        ttl_seconds: float = 3600.0,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._capacity = capacity
        self._ttl = ttl_seconds
        self._clock = clock or time.monotonic
        # OrderedDict preserves insertion order; we move to end on access
        # Maps user_id -> (UserProfile, expiry_timestamp)
        self._store: OrderedDict[str, tuple[UserProfile, float]] = OrderedDict()

    @property
    def capacity(self) -> int:
        """Return the maximum capacity of the cache."""
        return self._capacity

    def __len__(self) -> int:
        """Return the number of entries currently in the cache."""
        return len(self._store)

    def put(self, user_profile: UserProfile) -> None:
        """Add or update a user profile in the cache.

        If the cache is at capacity and the user_id is not already present,
        the least recently used entry is evicted to make room.

        Args:
            user_profile: The user profile to cache.
        """
        user_id = user_profile.user_id
        expiry = self._clock() + self._ttl

        if user_id in self._store:
            # Update existing entry and move to most-recently-used position
            self._store.move_to_end(user_id)
            self._store[user_id] = (user_profile, expiry)
        else:
            # Evict LRU entry if at capacity
            if len(self._store) >= self._capacity:
                self._store.popitem(last=False)
            self._store[user_id] = (user_profile, expiry)

    def get(self, user_id: str) -> Optional[UserProfile]:
        """Retrieve a user profile by user_id.

        Returns None if the entry is not found or has expired.
        Accessing an entry marks it as most recently used.

        Args:
            user_id: The user ID to look up.

        Returns:
            The cached UserProfile, or None if not found or expired.
        """
        entry = self._store.get(user_id)
        if entry is None:
            return None

        profile, expiry = entry
        if self._clock() >= expiry:
            # Entry has expired; remove it
            del self._store[user_id]
            return None

        # Mark as recently used
        self._store.move_to_end(user_id)
        return profile

    def get_batch(self, user_ids: list[str]) -> dict[str, UserProfile]:
        """Retrieve multiple user profiles in a single operation.

        Only returns entries that are found and not expired.
        Each accessed entry is marked as most recently used.

        Args:
            user_ids: List of user IDs to look up.

        Returns:
            Dictionary mapping user_id to UserProfile for found entries.
        """
        results: dict[str, UserProfile] = {}
        for user_id in user_ids:
            profile = self.get(user_id)
            if profile is not None:
                results[user_id] = profile
        return results
