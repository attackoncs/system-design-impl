"""Redis storage backend for rate limiter state."""

from __future__ import annotations

from typing import Any
import math

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from rate_limiter.storage import base as storage_base
from rate_limiter.storage.clocks import with_server_clock


class RedisStorage(storage_base.BaseStorage):
    """Redis-backed storage using redis-py.

    Uses pipelines for multi-step operations and Lua script evaluation for
    atomic execute_atomic calls.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        *,
        socket_timeout: float = 2,
        max_connections: int = 64,
    ) -> None:
        """Initialize Redis storage.

        Args:
            redis_url: Redis connection URL. Defaults to localhost:6379.
        """
        if not math.isfinite(socket_timeout) or not 0 < socket_timeout <= 60:
            raise ValueError("socket_timeout must be in (0, 60]")
        if type(max_connections) is not int or max_connections <= 0:
            raise ValueError("max_connections must be a positive integer")
        self._client: redis.Redis = redis.Redis.from_url(
            redis_url,
            decode_responses=False,
            socket_connect_timeout=socket_timeout,
            socket_timeout=socket_timeout,
            max_connections=max_connections,
            retry=Retry(NoBackoff(), 0),
            retry_on_error=[],
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

    def execute_atomic(self, script: str, keys: list[str], args: list[str]) -> Any:
        """Execute a Lua script atomically on Redis.

        Args:
            script: The Lua script string to execute.
            keys: List of keys involved in the operation.
            args: List of arguments for the operation.

        Returns:
            The result of the Lua script execution.
        """
        return self._client.eval(script, len(keys), *keys, *args)

    def execute_algorithm(self, lua_script, memory_script, keys, args):
        return self.execute_atomic(
            with_server_clock(lua_script), [*keys, keys[0] + ":clock-v2"], args
        )

    def close(self) -> None:
        """Release this instance's connection pool."""
        self._client.close()
