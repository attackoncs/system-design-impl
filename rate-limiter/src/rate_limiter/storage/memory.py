"""In-memory storage backend with thread safety and TTL support."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .base import BaseStorage


class MemoryStorage(BaseStorage):
    """Thread-safe in-memory storage backend.

    Uses a threading lock to ensure atomic operations and supports
    TTL-based key expiration via expiry timestamps.
    """

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._expiry: dict[str, float] = {}
        self._lock = threading.Lock()

    def _is_expired(self, key: str) -> bool:
        """Check if a key has expired.

        Args:
            key: The storage key to check.

        Returns:
            True if the key exists in _expiry and its expiry time has passed.
        """
        if key in self._expiry:
            return time.time() > self._expiry[key]
        return False

    def _cleanup_expired(self) -> None:
        """Remove all expired keys from storage.

        This method should be called under the lock.
        """
        now = time.time()
        expired_keys = [
            key for key, expiry in self._expiry.items() if now > expiry
        ]
        for key in expired_keys:
            self._data.pop(key, None)
            self._expiry.pop(key, None)

    def get(self, key: str) -> str | None:
        """Get a value by key, returning None if expired or missing.

        Args:
            key: The storage key to look up.

        Returns:
            The stored value as a string, or None if the key does not
            exist or has expired.
        """
        with self._lock:
            if self._is_expired(key):
                self._data.pop(key, None)
                self._expiry.pop(key, None)
                return None
            return self._data.get(key)

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        """Set a value with optional TTL.

        Args:
            key: The storage key.
            value: The value to store.
            ttl: Time-to-live in seconds. None means no expiration.
        """
        with self._lock:
            self._data[key] = value
            if ttl is not None:
                self._expiry[key] = time.time() + ttl
            else:
                self._expiry.pop(key, None)

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
            if self._is_expired(key):
                self._data.pop(key, None)
                self._expiry.pop(key, None)

            current = int(self._data.get(key, "0"))
            new_value = current + amount
            self._data[key] = str(new_value)

            if ttl is not None:
                self._expiry[key] = time.time() + ttl

            return new_value

    def execute_atomic(
        self, script: Any, keys: list[str], args: list[str]
    ) -> Any:
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
                f"MemoryStorage.execute_atomic requires a callable, got {type(script).__name__}"
            )

        with self._lock:
            return script(keys, args, self)

    def _get_raw(self, key: str) -> str | None:
        """Get a value without acquiring the lock (for use inside execute_atomic).

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

    def _set_raw(self, key: str, value: str, ttl: int | None = None) -> None:
        """Set a value without acquiring the lock (for use inside execute_atomic).

        Args:
            key: The storage key.
            value: The value to store.
            ttl: Time-to-live in seconds. None means no expiration.
        """
        self._data[key] = value
        if ttl is not None:
            self._expiry[key] = time.time() + ttl
        else:
            self._expiry.pop(key, None)

    def _increment_raw(self, key: str, amount: int = 1, ttl: int | None = None) -> int:
        """Increment without acquiring the lock (for use inside execute_atomic).

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
        self._data[key] = str(new_value)

        if ttl is not None:
            self._expiry[key] = time.time() + ttl

        return new_value
