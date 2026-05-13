"""Base storage interface for rate limiter backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseStorage(ABC):
    """Abstract base class for storage backends.

    All storage backends must implement get, set, increment, and
    execute_atomic methods. Implementations must be thread-safe
    for concurrent access.
    """

    @abstractmethod
    def get(self, key: str) -> str | None:
        """Get a value by key.

        Args:
            key: The storage key to look up.

        Returns:
            The stored value as a string, or None if the key does not exist.
        """
        ...

    @abstractmethod
    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        """Set a value with optional TTL.

        Args:
            key: The storage key.
            value: The value to store.
            ttl: Time-to-live in seconds. None means no expiration.
        """
        ...

    @abstractmethod
    def increment(self, key: str, amount: int = 1, ttl: int | None = None) -> int:
        """Atomically increment a counter and return the new value.

        If the key does not exist, it is initialized to 0 before
        incrementing.

        Args:
            key: The storage key to increment.
            amount: The amount to increment by.
            ttl: Time-to-live in seconds for the key. None means no expiration.

        Returns:
            The new value after incrementing.
        """
        ...

    @abstractmethod
    def execute_atomic(
        self, script: str, keys: list[str], args: list[str]
    ) -> Any:
        """Execute an atomic operation.

        For Redis backends, this executes a Lua script. For in-memory
        backends, this executes a callable under a lock.

        Args:
            script: The script or operation identifier to execute.
            keys: List of keys involved in the operation.
            args: List of arguments for the operation.

        Returns:
            The result of the atomic operation.
        """
        ...
