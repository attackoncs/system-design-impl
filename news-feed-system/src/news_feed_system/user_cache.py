"""Thread-safe user profile LRU with monotonic TTL expiration."""

from __future__ import annotations

import collections
import math
import threading
import time
from collections.abc import Callable

from news_feed_system.models import UserProfile


class UserCache:
    def __init__(
        self,
        capacity: int = 5000,
        ttl_seconds: float = 3600.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        if (
            isinstance(ttl_seconds, bool)
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be finite and positive")
        self._capacity = capacity
        self._ttl = ttl_seconds
        self._clock = clock or time.monotonic
        self._store: collections.OrderedDict[str, tuple[UserProfile, float]] = (
            collections.OrderedDict()
        )
        self._lock = threading.RLock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    def put(self, user_profile: UserProfile) -> None:
        with self._lock:
            self._store[user_profile.user_id] = (
                user_profile,
                self._clock() + self._ttl,
            )
            self._store.move_to_end(user_profile.user_id)
            if len(self._store) > self._capacity:
                self._store.popitem(last=False)

    def get(self, user_id: str) -> UserProfile | None:
        with self._lock:
            entry = self._store.get(user_id)
            if entry is None:
                return None
            profile, expiry = entry
            if self._clock() >= expiry:
                del self._store[user_id]
                return None
            self._store.move_to_end(user_id)
            return profile

    def get_batch(self, user_ids: list[str]) -> dict[str, UserProfile]:
        with self._lock:
            return {
                user_id: profile
                for user_id in user_ids
                if (profile := self.get(user_id)) is not None
            }
