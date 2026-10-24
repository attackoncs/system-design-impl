"""Bounded TTL social graph cache with coalesced reads and invalidation fences."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import math
import threading
import time
from collections.abc import Callable

from news_feed_system.social_graph import SocialGraph


class CachedSocialGraph:
    """Cache one backend per event loop; route relationship writes through here.

    Stripe locks coalesce identical misses and prevent a slow old read from
    repopulating cache after a completed follow mutation. Bounded generation
    counters also fence explicit synchronous invalidation during backend I/O.
    External backend changes become visible after TTL or explicit invalidation.
    """

    def __init__(
        self,
        backend: SocialGraph,
        ttl_seconds: float = 300.0,
        clock: Callable[[], float] | None = None,
        *,
        capacity: int = 5000,
    ) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be finite and positive")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        self._backend = backend
        self._ttl = ttl_seconds
        self._clock = clock or time.monotonic
        self._capacity = capacity
        self._follower_cache: collections.OrderedDict[
            str, tuple[tuple[str, ...], float]
        ] = collections.OrderedDict()
        self._following_cache: collections.OrderedDict[
            str, tuple[tuple[str, ...], float]
        ] = collections.OrderedDict()
        self._count_cache: collections.OrderedDict[str, tuple[int, float]] = (
            collections.OrderedDict()
        )
        self._locks = [asyncio.Lock() for _ in range(64)]
        self._generations = [0] * len(self._locks)
        self._cache_lock = threading.RLock()

    def _stripe(self, direction: str, user_id: str) -> int:
        return hash((direction, user_id)) % len(self._locks)

    async def _get(self, user_id: str, *, following: bool) -> list[str]:
        direction = "following" if following else "followers"
        cache = self._following_cache if following else self._follower_cache
        loader = (
            self._backend.get_following if following else self._backend.get_followers
        )
        stripe = self._stripe(direction, user_id)
        async with self._locks[stripe]:
            with self._cache_lock:
                cached = cache.get(user_id)
                if cached is not None and self._clock() < cached[1]:
                    cache.move_to_end(user_id)
                    return list(cached[0])
                cache.pop(user_id, None)
                generation = self._generations[stripe]
            result = tuple(sorted(set(await loader(user_id))))
            with self._cache_lock:
                if generation == self._generations[stripe]:
                    cache[user_id] = (result, self._clock() + self._ttl)
                    cache.move_to_end(user_id)
                    if len(cache) > self._capacity:
                        cache.popitem(last=False)
            return list(result)

    async def get_followers(self, user_id: str) -> list[str]:
        return await self._get(user_id, following=False)

    async def get_following(self, user_id: str) -> list[str]:
        return await self._get(user_id, following=True)

    async def get_follower_count(self, user_id: str) -> int:
        """Coalesce counts used to choose push/pull, retaining at most capacity IDs."""
        stripe = self._stripe("count", user_id)
        async with self._locks[stripe]:
            with self._cache_lock:
                cached = self._count_cache.get(user_id)
                if cached is not None and self._clock() < cached[1]:
                    self._count_cache.move_to_end(user_id)
                    return cached[0]
                self._count_cache.pop(user_id, None)
                generation = self._generations[stripe]
            result = await self._backend.get_follower_count(user_id)
            with self._cache_lock:
                if generation == self._generations[stripe]:
                    self._count_cache[user_id] = (result, self._clock() + self._ttl)
                    self._count_cache.move_to_end(user_id)
                    if len(self._count_cache) > self._capacity:
                        self._count_cache.popitem(last=False)
            return result

    async def is_following(self, follower_id: str, followee_id: str) -> bool:
        return await self._backend.is_following(follower_id, followee_id)

    async def _mutate(
        self, follower_id: str, followee_id: str, *, remove: bool
    ) -> None:
        # Deterministic acquisition avoids deadlock when opposite follows race.
        stripes = sorted(
            {
                self._stripe("followers", followee_id),
                self._stripe("following", follower_id),
                self._stripe("count", followee_id),
            }
        )
        async with contextlib.AsyncExitStack() as stack:
            for stripe in stripes:
                await stack.enter_async_context(self._locks[stripe])
            try:
                if remove:
                    await self._backend.remove_follow(follower_id, followee_id)
                else:
                    await self._backend.add_follow(follower_id, followee_id)
            finally:
                # Invalidate even if cancellation arrives after a backend committed.
                with self._cache_lock:
                    self._follower_cache.pop(followee_id, None)
                    self._following_cache.pop(follower_id, None)
                    self._count_cache.pop(followee_id, None)
                    for stripe in stripes:
                        self._generations[stripe] += 1

    async def add_follow(self, follower_id: str, followee_id: str) -> None:
        await self._mutate(follower_id, followee_id, remove=False)

    async def remove_follow(self, follower_id: str, followee_id: str) -> None:
        await self._mutate(follower_id, followee_id, remove=True)

    def invalidate(self, user_id: str) -> None:
        with self._cache_lock:
            self._follower_cache.pop(user_id, None)
            self._following_cache.pop(user_id, None)
            self._count_cache.pop(user_id, None)
            for stripe in {
                self._stripe("followers", user_id),
                self._stripe("following", user_id),
                self._stripe("count", user_id),
            }:
                self._generations[stripe] += 1
