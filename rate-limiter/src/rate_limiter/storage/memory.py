"""In-memory storage backend with thread safety and TTL support."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

from rate_limiter.storage import base as storage_base
from rate_limiter.storage.clocks import checked_time, delay_for_clock_regression


class MemoryStorage(storage_base.BaseStorage):
    """Thread-safe in-memory storage backend.

    Uses a reentrant lock so atomic callbacks can call public methods.
    TTL deadlines use a monotonic clock, independent of wall-clock changes.
    Callbacks must be short and must not wait for another storage operation.
    Atomicity provides mutual exclusion, not rollback on callback failure.
    """

    def __init__(self, *, max_keys: int = 100_000, clock=None) -> None:
        if type(max_keys) is not int or max_keys < 4:
            raise ValueError("max_keys must be an integer >= 4")
        self._data: dict[str, str] = {}
        self._expiry: dict[str, float] = OrderedDict()
        self._lock = threading.RLock()
        self._max_keys = max_keys
        self._clock = clock
        self._next_sweep = 0.0

    def _sweep(self, budget: int = 64, *, force: bool = False) -> None:
        """Visit a bounded number of deadlines, including otherwise idle keys."""
        now = time.monotonic()
        if not force and now < self._next_sweep:
            return
        self._next_sweep = now + 0.05
        for _ in range(min(budget, len(self._expiry))):
            key, expiry = self._expiry.popitem(last=False)
            if now >= expiry:
                self._data.pop(key, None)
            else:
                self._expiry[key] = expiry

    def _is_expired(self, key: str) -> bool:
        """Check if a key has expired.

        Args:
            key: The storage key to check.

        Returns:
            True if the key exists in _expiry and its expiry time has passed.
        """
        if key in self._expiry:
            return time.monotonic() >= self._expiry[key]
        return False

    def get(self, key: str) -> str | None:
        """Get a value by key, returning None if expired or missing.

        Args:
            key: The storage key to look up.

        Returns:
            The stored value as a string, or None if the key does not
            exist or has expired.
        """
        with self._lock:
            self._sweep()
            return self.get_unlocked(key)

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        """Set a value with optional TTL.

        Args:
            key: The storage key.
            value: The value to store.
            ttl: Time-to-live in seconds. None means no expiration.
        """
        with self._lock:
            self._sweep()
            self.set_unlocked(key, value, ttl)

    def increment(self, key: str, amount: int = 1, ttl: int | None = None) -> int:
        """Atomically increment a counter and return the new value.

        If the key does not exist or has expired, it is initialized to 0
        before incrementing.

        Args:
            key: The storage key to increment.
            amount: The amount to increment by.
            ttl: Time-to-live in seconds for the key. None means no expiration.

        Returns:
            The new value after incrementing.
        """
        with self._lock:
            return self._increment_raw(key, amount, ttl)

    def execute_algorithm(
        self,
        lua_script: str,
        memory_script: Any,
        keys: list[str],
        args: list[str],
    ) -> Any:
        """Execute the Python implementation of an atomic algorithm."""
        with self._lock:
            self._sweep()
            args = list(args)
            wall = checked_time(
                self._clock() if self._clock is not None else time.time()
            )
            clock_key = keys[0] + ":clock-v2"
            previous = self.get_unlocked(clock_key)
            now = max(wall, checked_time(previous)) if previous is not None else wall
            args[2] = str(now)
            result = memory_script(keys, args, self)
            self.set_unlocked(clock_key, str(now), int(args[-1]))
            return delay_for_clock_regression(result, now - wall)

    def execute_atomic(self, script: Any, keys: list[str], args: list[str]) -> Any:
        """Execute an atomic operation under the lock.

        For the in-memory backend, `script` should be a callable that
        receives (keys, args, storage_instance) and returns a result.
        The entire operation runs under the threading lock to ensure
        atomicity.

        Args:
            script: A callable that takes (keys, args, storage) and
                returns the operation result.
            keys: List of keys involved in the operation.
            args: List of arguments for the operation.

        Returns:
            The result of the callable.

        Raises:
            TypeError: If script is not callable.
        """
        if not callable(script):
            raise TypeError(
                "MemoryStorage.execute_atomic requires a callable, got"
                f" {type(script).__name__}"
            )

        with self._lock:
            self._sweep()
            return script(keys, args, self)

    def get_unlocked(self, key: str) -> str | None:
        """Get a value without acquiring the lock (for use inside
        execute_atomic).

        Args:
            key: The storage key to look up.

        Returns:
            The stored value as a string, or None if expired or missing.
        """
        if self._is_expired(key):
            self._data.pop(key, None)
            self._expiry.pop(key, None)
            return None
        return self._data.get(key)

    def set_unlocked(self, key: str, value: str, ttl: int | None = None) -> None:
        """Set a value without acquiring the lock (for use inside
        execute_atomic).

        Args:
            key: The storage key.
            value: The value to store.
            ttl: Time-to-live in seconds. None means no expiration.
        """
        if ttl is not None and (type(ttl) is not int or ttl <= 0):
            raise ValueError("ttl must be a positive integer")
        if key not in self._data and len(self._data) >= self._max_keys:
            # Bound pressure cleanup too; never scan the complete live map or
            # evict a live quota to admit a new subject.
            self._sweep(128, force=True)
            if len(self._data) >= self._max_keys:
                raise MemoryError("rate limiter state capacity reached")
        self._data[key] = value
        if ttl is not None:
            self._expiry[key] = time.monotonic() + ttl
        else:
            self._expiry.pop(key, None)

    def _increment_raw(self, key: str, amount: int = 1, ttl: int | None = None) -> int:
        """Increment without acquiring the lock (for use inside
        execute_atomic).

        Args:
            key: The storage key to increment.
            amount: The amount to increment by.
            ttl: Time-to-live in seconds. None means no expiration.

        Returns:
            The new value after incrementing.
        """
        if self._is_expired(key):
            self._data.pop(key, None)
            self._expiry.pop(key, None)

        current = int(self._data.get(key, "0"))
        new_value = current + amount
        # Match Redis INCRBY: preserve an existing TTL unless one is supplied.
        retained_ttl = self._expiry.get(key) if ttl is None else None
        self.set_unlocked(key, str(new_value), ttl)
        if retained_ttl is not None:
            self._expiry[key] = retained_ttl

        return new_value
