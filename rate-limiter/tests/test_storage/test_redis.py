"""Unit tests for RedisStorage backend using fakeredis."""

from __future__ import annotations

from unittest.mock import patch

import fakeredis
import pytest

from rate_limiter.storage.redis import RedisStorage


@pytest.fixture
def redis_storage() -> RedisStorage:
    """Create a RedisStorage instance backed by fakeredis."""
    fake_redis = fakeredis.FakeRedis(decode_responses=False)
    with patch("redis.Redis.from_url", return_value=fake_redis):
        storage = RedisStorage(redis_url="redis://localhost:6379")
    return storage


class TestRedisStorageGet:
    """Tests for RedisStorage.get method."""

    def test_get_nonexistent_key(self, redis_storage: RedisStorage):
        """Getting a key that was never set returns None."""
        assert redis_storage.get("nonexistent") is None

    def test_set_and_get(self, redis_storage: RedisStorage):
        """Basic set followed by get returns the stored value."""
        redis_storage.set("key1", "value1")
        assert redis_storage.get("key1") == "value1"


class TestRedisStorageTTL:
    """Tests for TTL behavior."""

    def test_set_with_ttl(self, redis_storage: RedisStorage):
        """Value is stored when TTL is specified."""
        redis_storage.set("key1", "value1", ttl=60)
        assert redis_storage.get("key1") == "value1"


class TestRedisStorageIncrement:
    """Tests for RedisStorage.increment method."""

    def test_increment_new_key(self, redis_storage: RedisStorage):
        """Incrementing a non-existent key starts from 0 and returns 1."""
        result = redis_storage.increment("counter")
        assert result == 1

    def test_increment_existing_key(self, redis_storage: RedisStorage):
        """Incrementing an existing key increases the value correctly."""
        redis_storage.increment("counter")
        redis_storage.increment("counter")
        result = redis_storage.increment("counter")
        assert result == 3

    def test_increment_with_ttl(self, redis_storage: RedisStorage):
        """Increment with TTL sets expiration on the key."""
        redis_storage.increment("counter", ttl=120)
        # Verify the value was incremented
        assert redis_storage.get("counter") == "1"
        # Verify TTL was set (fakeredis supports ttl command)
        ttl_value = redis_storage._client.ttl("counter")
        assert ttl_value > 0


class TestRedisStorageExecuteAtomic:
    """Tests for RedisStorage.execute_atomic (Lua script execution)."""

    def test_execute_atomic_lua_script(self, redis_storage: RedisStorage):
        """A simple Lua script executes correctly via eval."""
        script = "return redis.call('SET', KEYS[1], ARGV[1])"
        result = redis_storage.execute_atomic(script, ["lua_key"], ["lua_value"])
        assert result == b"OK"
        assert redis_storage.get("lua_key") == "lua_value"
