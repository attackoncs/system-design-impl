"""Social graph abstraction for managing follower/following relationships.

Provides an abstract interface (SocialGraph ABC) and a default in-memory
implementation (InMemorySocialGraph) for testing and development.
"""

from __future__ import annotations

import abc
import collections
import threading
from typing import Dict, Set


class SocialGraph(abc.ABC):
    """Abstract interface for social relationship management.

    Manages follower/following relationships between users. Implementations can
    be backed by any graph storage.
    """

    @abc.abstractmethod
    async def add_follow(self, follower_id: str, followee_id: str) -> None:
        """Add a follower relationship."""

    @abc.abstractmethod
    async def remove_follow(self, follower_id: str, followee_id: str) -> None:
        """Remove a follower relationship."""

    @abc.abstractmethod
    async def get_followers(self, user_id: str) -> list[str]:
        """Get all follower IDs for a user."""

    @abc.abstractmethod
    async def get_following(self, user_id: str) -> list[str]:
        """Get all user IDs that a user follows."""

    @abc.abstractmethod
    async def get_follower_count(self, user_id: str) -> int:
        """Get the follower count for a user."""

    @abc.abstractmethod
    async def is_following(self, follower_id: str, followee_id: str) -> bool:
        """Check if a follower relationship exists."""


class InMemorySocialGraph(SocialGraph):
    """In-memory implementation of the social graph for testing."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # user_id -> set of follower_ids
        self._followers: Dict[str, Set[str]] = collections.defaultdict(set)
        # user_id -> set of following_ids
        self._following: Dict[str, Set[str]] = collections.defaultdict(set)

    async def add_follow(self, follower_id: str, followee_id: str) -> None:
        with self._lock:
            self._followers[followee_id].add(follower_id)
            self._following[follower_id].add(followee_id)

    async def remove_follow(self, follower_id: str, followee_id: str) -> None:
        with self._lock:
            for index, key, value in (
                (self._followers, followee_id, follower_id),
                (self._following, follower_id, followee_id),
            ):
                values = index.get(key)
                if values is not None:
                    values.discard(value)
                    if not values:
                        del index[key]

    async def get_followers(self, user_id: str) -> list[str]:
        with self._lock:
            return sorted(self._followers.get(user_id, ()))

    async def get_following(self, user_id: str) -> list[str]:
        with self._lock:
            return sorted(self._following.get(user_id, ()))

    async def get_follower_count(self, user_id: str) -> int:
        with self._lock:
            return len(self._followers.get(user_id, ()))

    async def is_following(self, follower_id: str, followee_id: str) -> bool:
        with self._lock:
            return follower_id in self._followers.get(followee_id, ())
