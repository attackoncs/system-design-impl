"""Bounded hot/normal LRU tiers backed by an authoritative post store."""

from __future__ import annotations

import collections
import datetime
import threading

from news_feed_system.models import Post
from news_feed_system.storage import InMemoryPostStore, PostStore, snapshot as _snapshot


class PostCache:
    """Write through to storage; cache eviction never deletes source posts.

    All tier moves are protected together. Public values are snapshots so
    mutating a caller's media or sharing list cannot change accepted content.
    The default store is process-local; pass SQLitePostStore for restart recovery.
    """

    def __init__(
        self,
        hot_capacity: int = 1000,
        normal_capacity: int = 10000,
        access_threshold: int = 5,
        *,
        store: PostStore | None = None,
    ) -> None:
        for name, value in (
            ("hot_capacity", hot_capacity),
            ("normal_capacity", normal_capacity),
            ("access_threshold", access_threshold),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self._hot_capacity = hot_capacity
        self._normal_capacity = normal_capacity
        self._access_threshold = access_threshold
        self._store = store if store is not None else InMemoryPostStore()
        self._hot: collections.OrderedDict[str, Post] = collections.OrderedDict()
        self._normal: collections.OrderedDict[str, Post] = collections.OrderedDict()
        self._access_counts: dict[str, int] = {}
        self._lock = threading.RLock()

    @property
    def store(self) -> PostStore:
        return self._store

    @property
    def hot_capacity(self) -> int:
        return self._hot_capacity

    @property
    def normal_capacity(self) -> int:
        return self._normal_capacity

    @property
    def access_threshold(self) -> int:
        return self._access_threshold

    @property
    def hot_size(self) -> int:
        with self._lock:
            return len(self._hot)

    @property
    def normal_size(self) -> int:
        with self._lock:
            return len(self._normal)

    def put(self, post: Post) -> None:
        snapshot = _snapshot(post)
        self._store.put(snapshot)
        with self._lock:
            self._cache_put(snapshot)

    def _cache_put(self, post: Post) -> None:
        post_id = post.post_id
        self._normal.pop(post_id, None)
        self._access_counts.pop(post_id, None)
        if post_id not in self._hot and len(self._hot) >= self._hot_capacity:
            self._demote_lru_from_hot()
        self._hot[post_id] = post
        self._hot.move_to_end(post_id)

    def get(self, post_id: str) -> Post | None:
        with self._lock:
            if post_id in self._hot:
                self._hot.move_to_end(post_id)
                return _snapshot(self._hot[post_id])
            if post_id in self._normal:
                self._normal.move_to_end(post_id)
                count = self._access_counts.get(post_id, 0) + 1
                self._access_counts[post_id] = count
                post = self._normal[post_id]
                if count >= self._access_threshold:
                    self._normal.pop(post_id)
                    self._cache_put(post)
                return _snapshot(post)
        # Storage can block; keep unrelated cache hits available during I/O.
        post = self._store.get(post_id)
        if post is None:
            return None
        snapshot = _snapshot(post)
        with self._lock:
            self._cache_put(snapshot)
        return _snapshot(snapshot)

    def get_by_author(
        self,
        author_id: str,
        limit: int = 20,
        before: tuple[datetime.datetime, str] | None = None,
    ) -> list[Post]:
        posts = self._store.get_by_author(author_id, limit=limit, before=before)
        with self._lock:
            for post in reversed(posts):
                self._cache_put(_snapshot(post))
        return [_snapshot(post) for post in posts]

    def _demote_lru_from_hot(self) -> None:
        post_id, post = self._hot.popitem(last=False)
        if len(self._normal) >= self._normal_capacity:
            evicted_id, _ = self._normal.popitem(last=False)
            self._access_counts.pop(evicted_id, None)
        self._normal[post_id] = post
        self._access_counts[post_id] = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._hot) + len(self._normal)

    def contains(self, post_id: str) -> bool:
        with self._lock:
            return post_id in self._hot or post_id in self._normal

    def is_hot(self, post_id: str) -> bool:
        with self._lock:
            return post_id in self._hot

    def is_normal(self, post_id: str) -> bool:
        with self._lock:
            return post_id in self._normal
