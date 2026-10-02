"""Tests for the social graph cache layer."""

from __future__ import annotations

import pytest

from news_feed_system.social_graph import InMemorySocialGraph, SocialGraph
from news_feed_system.social_graph_cache import CachedSocialGraph


class SpyingSocialGraph(SocialGraph):
    """A social graph wrapper that counts backend calls for testing."""

    def __init__(self, backend: InMemorySocialGraph) -> None:
        self._backend = backend
        self.get_followers_calls = 0
        self.get_following_calls = 0
        self.get_follower_count_calls = 0
        self.is_following_calls = 0

    async def add_follow(self, follower_id: str, followee_id: str) -> None:
        await self._backend.add_follow(follower_id, followee_id)

    async def remove_follow(self, follower_id: str, followee_id: str) -> None:
        await self._backend.remove_follow(follower_id, followee_id)

    async def get_followers(self, user_id: str) -> list[str]:
        self.get_followers_calls += 1
        return await self._backend.get_followers(user_id)

    async def get_following(self, user_id: str) -> list[str]:
        self.get_following_calls += 1
        return await self._backend.get_following(user_id)

    async def get_follower_count(self, user_id: str) -> int:
        self.get_follower_count_calls += 1
        return await self._backend.get_follower_count(user_id)

    async def is_following(self, follower_id: str, followee_id: str) -> bool:
        self.is_following_calls += 1
        return await self._backend.is_following(follower_id, followee_id)


@pytest.fixture
def backend():
    """Create a fresh InMemorySocialGraph backend."""
    return InMemorySocialGraph()


@pytest.fixture
def spy(backend):
    """Create a spying wrapper around the backend."""
    return SpyingSocialGraph(backend)


@pytest.fixture
def clock():
    """Create a controllable clock for TTL testing."""
    current_time = [0.0]

    def _clock():
        return current_time[0]

    _clock.advance = lambda seconds: current_time.__setitem__(0, current_time[0] + seconds)
    _clock.set = lambda t: current_time.__setitem__(0, t)
    return _clock


@pytest.fixture
def cached_graph(spy, clock):
    """Create a CachedSocialGraph with controllable clock and 60s TTL."""
    return CachedSocialGraph(backend=spy, ttl_seconds=60.0, clock=clock)


class TestCacheHitReturnsWithoutBackendCall:
    """Test that cache hits return cached data without calling the backend."""

    async def test_get_followers_cache_hit(self, cached_graph, spy, backend):
        """Second get_followers call uses cache, not backend."""
        await backend.add_follow("alice", "bob")

        # First call populates cache
        result1 = await cached_graph.get_followers("bob")
        assert spy.get_followers_calls == 1
        assert "alice" in result1

        # Second call should use cache
        result2 = await cached_graph.get_followers("bob")
        assert spy.get_followers_calls == 1  # No additional backend call
        assert result2 == result1

    async def test_get_following_cache_hit(self, cached_graph, spy, backend):
        """Second get_following call uses cache, not backend."""
        await backend.add_follow("alice", "bob")

        # First call populates cache
        result1 = await cached_graph.get_following("alice")
        assert spy.get_following_calls == 1
        assert "bob" in result1

        # Second call should use cache
        result2 = await cached_graph.get_following("alice")
        assert spy.get_following_calls == 1  # No additional backend call
        assert result2 == result1

    async def test_separate_caches_for_follower_and_following(self, cached_graph, spy, backend):
        """Follower and following caches are independent."""
        await backend.add_follow("alice", "bob")

        await cached_graph.get_followers("bob")
        await cached_graph.get_following("alice")

        assert spy.get_followers_calls == 1
        assert spy.get_following_calls == 1

        # Cache hits for both
        await cached_graph.get_followers("bob")
        await cached_graph.get_following("alice")

        assert spy.get_followers_calls == 1
        assert spy.get_following_calls == 1

    async def test_different_users_cached_independently(self, cached_graph, spy, backend):
        """Cache entries for different users are independent."""
        await backend.add_follow("alice", "bob")
        await backend.add_follow("charlie", "dave")

        await cached_graph.get_followers("bob")
        await cached_graph.get_followers("dave")
        assert spy.get_followers_calls == 2

        # Both should be cached now
        await cached_graph.get_followers("bob")
        await cached_graph.get_followers("dave")
        assert spy.get_followers_calls == 2


class TestCacheInvalidationOnMutations:
    """Test that cache entries are invalidated when relationships change."""

    async def test_add_follow_invalidates_followee_follower_cache(self, cached_graph, spy, backend):
        """add_follow invalidates the followee's follower cache."""
        await backend.add_follow("alice", "bob")

        # Populate cache
        await cached_graph.get_followers("bob")
        assert spy.get_followers_calls == 1

        # Add a new follow through the cached graph
        await cached_graph.add_follow("charlie", "bob")

        # Cache should be invalidated, so this hits backend
        result = await cached_graph.get_followers("bob")
        assert spy.get_followers_calls == 2
        assert set(result) == {"alice", "charlie"}

    async def test_add_follow_invalidates_follower_following_cache(self, cached_graph, spy, backend):
        """add_follow invalidates the follower's following cache."""
        await backend.add_follow("alice", "bob")

        # Populate cache
        await cached_graph.get_following("alice")
        assert spy.get_following_calls == 1

        # Add a new follow through the cached graph
        await cached_graph.add_follow("alice", "charlie")

        # Cache should be invalidated, so this hits backend
        result = await cached_graph.get_following("alice")
        assert spy.get_following_calls == 2
        assert set(result) == {"bob", "charlie"}

    async def test_remove_follow_invalidates_followee_follower_cache(self, cached_graph, spy, backend):
        """remove_follow invalidates the followee's follower cache."""
        await backend.add_follow("alice", "bob")
        await backend.add_follow("charlie", "bob")

        # Populate cache
        await cached_graph.get_followers("bob")
        assert spy.get_followers_calls == 1

        # Remove follow through the cached graph
        await cached_graph.remove_follow("alice", "bob")

        # Cache should be invalidated
        result = await cached_graph.get_followers("bob")
        assert spy.get_followers_calls == 2
        assert result == ["charlie"]

    async def test_remove_follow_invalidates_follower_following_cache(self, cached_graph, spy, backend):
        """remove_follow invalidates the follower's following cache."""
        await backend.add_follow("alice", "bob")
        await backend.add_follow("alice", "charlie")

        # Populate cache
        await cached_graph.get_following("alice")
        assert spy.get_following_calls == 1

        # Remove follow through the cached graph
        await cached_graph.remove_follow("alice", "bob")

        # Cache should be invalidated
        result = await cached_graph.get_following("alice")
        assert spy.get_following_calls == 2
        assert result == ["charlie"]

    async def test_manual_invalidate_clears_both_caches(self, cached_graph, spy, backend):
        """invalidate() clears both follower and following caches for a user."""
        await backend.add_follow("alice", "bob")

        # Populate both caches for bob
        await cached_graph.get_followers("bob")
        await cached_graph.get_following("bob")
        assert spy.get_followers_calls == 1
        assert spy.get_following_calls == 1

        # Manual invalidation
        cached_graph.invalidate("bob")

        # Both should hit backend again
        await cached_graph.get_followers("bob")
        await cached_graph.get_following("bob")
        assert spy.get_followers_calls == 2
        assert spy.get_following_calls == 2

    async def test_mutation_does_not_invalidate_unrelated_users(self, cached_graph, spy, backend):
        """Mutations only invalidate affected users' caches."""
        await backend.add_follow("alice", "bob")
        await backend.add_follow("charlie", "dave")

        # Populate caches for both
        await cached_graph.get_followers("bob")
        await cached_graph.get_followers("dave")
        assert spy.get_followers_calls == 2

        # Mutate bob's followers
        await cached_graph.add_follow("eve", "bob")

        # dave's cache should still be valid
        await cached_graph.get_followers("dave")
        assert spy.get_followers_calls == 2  # No new call for dave

        # bob's cache should be invalidated
        await cached_graph.get_followers("bob")
        assert spy.get_followers_calls == 3


class TestTTLExpirationTriggersBackendRefresh:
    """Test that expired cache entries trigger a backend refresh."""

    async def test_follower_cache_expires_after_ttl(self, cached_graph, spy, backend, clock):
        """Follower cache entry expires after TTL and refreshes from backend."""
        await backend.add_follow("alice", "bob")

        # Populate cache at time 0
        await cached_graph.get_followers("bob")
        assert spy.get_followers_calls == 1

        # Still cached before TTL
        clock.advance(59.0)
        await cached_graph.get_followers("bob")
        assert spy.get_followers_calls == 1

        # Expired after TTL
        clock.advance(2.0)  # Now at 61s, past 60s TTL
        await cached_graph.get_followers("bob")
        assert spy.get_followers_calls == 2

    async def test_following_cache_expires_after_ttl(self, cached_graph, spy, backend, clock):
        """Following cache entry expires after TTL and refreshes from backend."""
        await backend.add_follow("alice", "bob")

        # Populate cache at time 0
        await cached_graph.get_following("alice")
        assert spy.get_following_calls == 1

        # Still cached before TTL
        clock.advance(59.0)
        await cached_graph.get_following("alice")
        assert spy.get_following_calls == 1

        # Expired after TTL
        clock.advance(2.0)  # Now at 61s, past 60s TTL
        await cached_graph.get_following("alice")
        assert spy.get_following_calls == 2

    async def test_expired_cache_returns_fresh_data(self, cached_graph, spy, backend, clock):
        """After TTL expiration, fresh data from backend is returned."""
        await backend.add_follow("alice", "bob")

        # Populate cache
        result1 = await cached_graph.get_followers("bob")
        assert result1 == ["alice"]

        # Modify backend directly (simulating external change)
        await backend.add_follow("charlie", "bob")

        # Cache still returns stale data before TTL
        clock.advance(30.0)
        result2 = await cached_graph.get_followers("bob")
        assert result2 == ["alice"]  # Stale

        # After TTL, fresh data is returned
        clock.advance(31.0)  # Now at 61s
        result3 = await cached_graph.get_followers("bob")
        assert set(result3) == {"alice", "charlie"}  # Fresh

    async def test_ttl_refresh_recaches_with_new_expiry(self, cached_graph, spy, backend, clock):
        """After TTL refresh, the new entry has a fresh TTL."""
        await backend.add_follow("alice", "bob")

        # Populate cache at time 0
        await cached_graph.get_followers("bob")
        assert spy.get_followers_calls == 1

        # Expire and refresh at time 61
        clock.advance(61.0)
        await cached_graph.get_followers("bob")
        assert spy.get_followers_calls == 2

        # New cache entry should be valid for another 60s
        clock.advance(59.0)  # Now at 120s
        await cached_graph.get_followers("bob")
        assert spy.get_followers_calls == 2  # Still cached

        # Expires again at 121s from refresh point
        clock.advance(2.0)  # Now at 122s
        await cached_graph.get_followers("bob")
        assert spy.get_followers_calls == 3


class TestCachedSocialGraphDelegation:
    """Test that non-cached methods delegate properly to backend."""

    async def test_get_follower_count_delegates(self, cached_graph, spy, backend):
        """get_follower_count delegates to backend."""
        await backend.add_follow("alice", "bob")
        await backend.add_follow("charlie", "bob")

        count = await cached_graph.get_follower_count("bob")
        assert count == 2
        assert spy.get_follower_count_calls == 1

    async def test_is_following_delegates(self, cached_graph, spy, backend):
        """is_following delegates to backend."""
        await backend.add_follow("alice", "bob")

        assert await cached_graph.is_following("alice", "bob")
        assert not await cached_graph.is_following("bob", "alice")
        assert spy.is_following_calls == 2
