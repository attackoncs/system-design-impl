"""Unit tests for the UserCache."""

from news_feed_system.models import UserProfile
from news_feed_system.user_cache import UserCache


def _make_profile(user_id: str, username: str = "", follower_count: int = 0) -> UserProfile:
    """Helper to create a UserProfile."""
    return UserProfile(
        user_id=user_id,
        username=username or f"user_{user_id}",
        profile_picture_url=f"https://example.com/{user_id}.png",
        follower_count=follower_count,
    )


class TestUserCachePutAndGet:
    """Tests for put and get operations."""

    def test_put_and_get_single_profile(self):
        cache = UserCache(capacity=10, ttl_seconds=60.0)
        profile = _make_profile("u1")
        cache.put(profile)
        assert cache.get("u1") == profile

    def test_get_nonexistent_returns_none(self):
        cache = UserCache(capacity=10, ttl_seconds=60.0)
        assert cache.get("nonexistent") is None

    def test_put_updates_existing_entry(self):
        cache = UserCache(capacity=10, ttl_seconds=60.0)
        profile_v1 = _make_profile("u1", username="old_name")
        profile_v2 = _make_profile("u1", username="new_name")
        cache.put(profile_v1)
        cache.put(profile_v2)
        result = cache.get("u1")
        assert result is not None
        assert result.username == "new_name"

    def test_len_reflects_entries(self):
        cache = UserCache(capacity=10, ttl_seconds=60.0)
        assert len(cache) == 0
        cache.put(_make_profile("u1"))
        assert len(cache) == 1
        cache.put(_make_profile("u2"))
        assert len(cache) == 2

    def test_put_same_user_does_not_increase_len(self):
        cache = UserCache(capacity=10, ttl_seconds=60.0)
        cache.put(_make_profile("u1", username="v1"))
        cache.put(_make_profile("u1", username="v2"))
        assert len(cache) == 1


class TestUserCacheLRUEviction:
    """Tests for LRU eviction when capacity is exceeded."""

    def test_evicts_lru_when_at_capacity(self):
        cache = UserCache(capacity=3, ttl_seconds=60.0)
        cache.put(_make_profile("u1"))
        cache.put(_make_profile("u2"))
        cache.put(_make_profile("u3"))
        # Cache is full; adding u4 should evict u1 (LRU)
        cache.put(_make_profile("u4"))

        assert cache.get("u1") is None
        assert cache.get("u2") is not None
        assert cache.get("u3") is not None
        assert cache.get("u4") is not None

    def test_access_prevents_eviction(self):
        cache = UserCache(capacity=3, ttl_seconds=60.0)
        cache.put(_make_profile("u1"))
        cache.put(_make_profile("u2"))
        cache.put(_make_profile("u3"))
        # Access u1 to make it recently used
        cache.get("u1")
        # Now add u4; u2 should be evicted (it's now LRU)
        cache.put(_make_profile("u4"))

        assert cache.get("u1") is not None
        assert cache.get("u2") is None
        assert cache.get("u3") is not None
        assert cache.get("u4") is not None

    def test_update_prevents_eviction(self):
        cache = UserCache(capacity=3, ttl_seconds=60.0)
        cache.put(_make_profile("u1"))
        cache.put(_make_profile("u2"))
        cache.put(_make_profile("u3"))
        # Update u1 to make it recently used
        cache.put(_make_profile("u1", username="updated"))
        # Now add u4; u2 should be evicted (it's now LRU)
        cache.put(_make_profile("u4"))

        assert cache.get("u1") is not None
        assert cache.get("u2") is None

    def test_capacity_property(self):
        cache = UserCache(capacity=42, ttl_seconds=60.0)
        assert cache.capacity == 42


class TestUserCacheTTLExpiration:
    """Tests for TTL-based expiration."""

    def test_entry_expires_after_ttl(self):
        current_time = 100.0

        def clock() -> float:
            return current_time

        cache = UserCache(capacity=10, ttl_seconds=60.0, clock=clock)
        cache.put(_make_profile("u1"))

        # Advance time past TTL
        current_time = 161.0
        assert cache.get("u1") is None

    def test_entry_valid_before_ttl(self):
        current_time = 100.0

        def clock() -> float:
            return current_time

        cache = UserCache(capacity=10, ttl_seconds=60.0, clock=clock)
        cache.put(_make_profile("u1"))

        # Advance time but stay within TTL
        current_time = 159.0
        assert cache.get("u1") is not None

    def test_expired_entry_removed_from_store(self):
        current_time = 100.0

        def clock() -> float:
            return current_time

        cache = UserCache(capacity=10, ttl_seconds=60.0, clock=clock)
        cache.put(_make_profile("u1"))
        assert len(cache) == 1

        # Expire the entry
        current_time = 161.0
        cache.get("u1")  # Triggers removal
        assert len(cache) == 0

    def test_put_refreshes_ttl(self):
        current_time = 100.0

        def clock() -> float:
            return current_time

        cache = UserCache(capacity=10, ttl_seconds=60.0, clock=clock)
        cache.put(_make_profile("u1"))

        # Advance time close to expiry
        current_time = 155.0
        # Re-put to refresh TTL
        cache.put(_make_profile("u1", username="refreshed"))

        # Advance past original TTL but within new TTL
        current_time = 210.0
        assert cache.get("u1") is not None


class TestUserCacheBatchRetrieval:
    """Tests for get_batch operation."""

    def test_get_batch_returns_found_entries(self):
        cache = UserCache(capacity=10, ttl_seconds=60.0)
        cache.put(_make_profile("u1"))
        cache.put(_make_profile("u2"))
        cache.put(_make_profile("u3"))

        result = cache.get_batch(["u1", "u2", "u3"])
        assert len(result) == 3
        assert "u1" in result
        assert "u2" in result
        assert "u3" in result

    def test_get_batch_skips_missing_entries(self):
        cache = UserCache(capacity=10, ttl_seconds=60.0)
        cache.put(_make_profile("u1"))
        cache.put(_make_profile("u3"))

        result = cache.get_batch(["u1", "u2", "u3"])
        assert len(result) == 2
        assert "u1" in result
        assert "u2" not in result
        assert "u3" in result

    def test_get_batch_skips_expired_entries(self):
        current_time = 100.0

        def clock() -> float:
            return current_time

        cache = UserCache(capacity=10, ttl_seconds=60.0, clock=clock)
        cache.put(_make_profile("u1"))

        current_time = 150.0
        cache.put(_make_profile("u2"))

        # Advance past u1's TTL but not u2's
        current_time = 161.0
        result = cache.get_batch(["u1", "u2"])
        assert len(result) == 1
        assert "u1" not in result
        assert "u2" in result

    def test_get_batch_empty_list(self):
        cache = UserCache(capacity=10, ttl_seconds=60.0)
        cache.put(_make_profile("u1"))
        result = cache.get_batch([])
        assert result == {}

    def test_get_batch_all_missing(self):
        cache = UserCache(capacity=10, ttl_seconds=60.0)
        result = cache.get_batch(["u1", "u2"])
        assert result == {}
