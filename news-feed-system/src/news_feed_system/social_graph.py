"""Social graph abstraction for managing follower/following relationships.

Provides an abstract interface (SocialGraph ABC) and a default in-memory
implementation (InMemorySocialGraph) for testing and development.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Dict, List, Set


class SocialGraph(ABC):
    """Abstract interface for social relationship management.

    Manages follower/following relationships between users.
    Implementations can be backed by any graph storage.
    """

    @abstractmethod
    async def add_follow(self, follower_id: str, followee_id: str) -> None:
        """Add a follower relationship."""
        ...

    @abstractmethod
    async def remove_follow(self, follower_id: str, followee_id: str) -> None:
        """Remove a follower relationship."""
        ...

    @abstractmethod
    async def get_followers(self, user_id: str) -> list[str]:
        """Get all follower IDs for a user."""
        ...

    @abstractmethod
    async def get_following(self, user_id: str) -> list[str]:
        """Get all user IDs that a user follows."""
        ...

    @abstractmethod
    async def get_follower_count(self, user_id: str) -> int:
        """Get the follower count for a user."""
        ...

    @abstractmethod
    async def is_following(self, follower_id: str, followee_id: str) -> bool:
        """Check if a follower relationship exists."""
        ...


class InMemorySocialGraph(SocialGraph):
    """In-memory implementation of the social graph for testing."""

    def __init__(self) -> None:
        # user_id -> set of follower_ids
        self._followers: Dict[str, Set[str]] = defaultdict(set)
        # user_id -> set of following_ids
        self._following: Dict[str, Set[str]] = defaultdict(set)

    async def add_follow(self, follower_id: str, followee_id: str) -> None:
        self._followers[followee_id].add(follower_id)
        self._following[follower_id].add(followee_id)

    async def remove_follow(self, follower_id: str, followee_id: str) -> None:
        self._followers[followee_id].discard(follower_id)
        self._following[follower_id].discard(followee_id)

    async def get_followers(self, user_id: str) -> list[str]:
        return list(self._followers[user_id])

    async def get_following(self, user_id: str) -> list[str]:
        return list(self._following[user_id])

    async def get_follower_count(self, user_id: str) -> int:
        return len(self._followers[user_id])

    async def is_following(self, follower_id: str, followee_id: str) -> bool:
        return follower_id in self._followers[followee_id]
