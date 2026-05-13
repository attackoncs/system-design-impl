"""Redis storage backend for rate limiter state."""

from __future__ import annotations

from typing import Any

import redis

from .base import BaseStorage


class RedisStorage(BaseStorage):
    """Redis-backed storage using redis-py.

    Uses pipelines for multi-step operations and Lua script
    evaluation for atomic execute_atomic calls.
    """

    def __init__(self, redis_url: str = "redis://localhost:6379") -> None:
        """Initialize Redis storage.

        Args:
            redis_url: Redis connection URL. Defaults to localhost:6379.
        """
        self._client: redis.Redis = redis.Redis.from_url(
            redis_url, decode_responses=False,
            socket_connect_timeout=2,
            socket_timeout=2,
        )

    def get(self, key: str) -> str | None:
        """Get a value by key.

        Args:
            key: The storage key to look up.

        Returns:
            The stored value as a string, or None if the key does not exist.
        """
        value = self._client.get(key)
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        """Set a value with optional TTL.

        Args:
            key: The storage key.
            value: The value to store.
            ttl: Time-to-live in seconds. None means no expiration.
        """
        if ttl is not None:
            self._client.set(key, value, ex=ttl)
        else:
            self._client.set(key, value)

    def increment(self, key: str, amount: int = 1, ttl: int | None = None) -> int:
        """Atomically increment a counter using a pipeline.

        If the key does not exist, Redis initializes it to 0 before
        incrementing.

        Args:
            key: The storage key to increment.
            amount: The amount to increment by.
            ttl: Time-to-live in seconds for the key. None means no expiration.

        Returns:
            The new value after incrementing.
        """
        pipe = self._client.pipeline()
        pipe.incrby(key, amount)
        if ttl is not None:
            pipe.expire(key, ttl)
        results = pipe.execute()
        # First result is the new value from INCRBY
        return int(results[0])

    def execute_atomic(
        self, script: str, keys: list[str], args: list[str]
    ) -> Any:
        """Execute a Lua script atomically on Redis.

        Args:
            script: The Lua script string to execute.
            keys: List of keys involved in the operation.
            args: List of arguments for the operation.

        Returns:
            The result of the Lua script execution.
        """
        return self._client.eval(script, len(keys), *keys, *args)
