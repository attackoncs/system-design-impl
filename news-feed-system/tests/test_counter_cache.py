"""Unit tests for the CounterCache module."""

from news_feed_system.counter_cache import CounterCache
from news_feed_system.models import CounterType


class TestCounterCacheGet:
    """Tests for counter retrieval and initialization."""

    def test_get_uninitialized_counter_returns_zero(self) -> None:
        cache = CounterCache()
        assert cache.get("post_1", CounterType.LIKE) == 0

    def test_get_returns_current_value_after_increments(self) -> None:
        cache = CounterCache()
        cache.increment("post_1", CounterType.LIKE)
        cache.increment("post_1", CounterType.LIKE)
        assert cache.get("post_1", CounterType.LIKE) == 2

    def test_get_different_counter_types_are_independent(self) -> None:
        cache = CounterCache()
        cache.increment("post_1", CounterType.LIKE)
        cache.increment("post_1", CounterType.LIKE)
        cache.increment("post_1", CounterType.REPLY)
        assert cache.get("post_1", CounterType.LIKE) == 2
        assert cache.get("post_1", CounterType.REPLY) == 1

    def test_get_different_entities_are_independent(self) -> None:
        cache = CounterCache()
        cache.increment("user_1", CounterType.FOLLOWER)
        cache.increment("user_2", CounterType.FOLLOWER)
        cache.increment("user_2", CounterType.FOLLOWER)
        assert cache.get("user_1", CounterType.FOLLOWER) == 1
        assert cache.get("user_2", CounterType.FOLLOWER) == 2


class TestCounterCacheIncrement:
    """Tests for atomic increment operations."""

    def test_increment_from_zero(self) -> None:
        cache = CounterCache()
        result = cache.increment("post_1", CounterType.LIKE)
        assert result == 1
        assert cache.get("post_1", CounterType.LIKE) == 1

    def test_increment_returns_new_value(self) -> None:
        cache = CounterCache()
        cache.increment("post_1", CounterType.LIKE)
        result = cache.increment("post_1", CounterType.LIKE)
        assert result == 2

    def test_increment_multiple_times(self) -> None:
        cache = CounterCache()
        for i in range(5):
            result = cache.increment("user_1", CounterType.FOLLOWING)
            assert result == i + 1
        assert cache.get("user_1", CounterType.FOLLOWING) == 5


class TestCounterCacheDecrement:
    """Tests for atomic decrement operations with zero floor."""

    def test_decrement_from_positive_value(self) -> None:
        cache = CounterCache()
        cache.increment("post_1", CounterType.LIKE)
        cache.increment("post_1", CounterType.LIKE)
        result = cache.decrement("post_1", CounterType.LIKE)
        assert result == 1

    def test_decrement_at_zero_stays_at_zero(self) -> None:
        cache = CounterCache()
        result = cache.decrement("post_1", CounterType.LIKE)
        assert result == 0
        assert cache.get("post_1", CounterType.LIKE) == 0

    def test_decrement_does_not_go_below_zero(self) -> None:
        cache = CounterCache()
        cache.increment("user_1", CounterType.FOLLOWER)
        cache.decrement("user_1", CounterType.FOLLOWER)
        result = cache.decrement("user_1", CounterType.FOLLOWER)
        assert result == 0

    def test_decrement_returns_new_value(self) -> None:
        cache = CounterCache()
        for _ in range(3):
            cache.increment("post_1", CounterType.REPLY)
        result = cache.decrement("post_1", CounterType.REPLY)
        assert result == 2


class TestCounterCacheGetBatch:
    """Tests for batch retrieval of multiple counters."""

    def test_batch_retrieval_returns_all_requested_counters(self) -> None:
        cache = CounterCache()
        cache.increment("post_1", CounterType.LIKE)
        cache.increment("post_1", CounterType.LIKE)
        cache.increment("post_1", CounterType.REPLY)
        cache.increment("user_1", CounterType.FOLLOWER)

        keys = [
            ("post_1", CounterType.LIKE),
            ("post_1", CounterType.REPLY),
            ("user_1", CounterType.FOLLOWER),
        ]
        result = cache.get_batch(keys)

        assert result == {
            ("post_1", CounterType.LIKE): 2,
            ("post_1", CounterType.REPLY): 1,
            ("user_1", CounterType.FOLLOWER): 1,
        }

    def test_batch_retrieval_returns_zero_for_missing_counters(self) -> None:
        cache = CounterCache()
        keys = [
            ("post_99", CounterType.LIKE),
            ("user_99", CounterType.FOLLOWING),
        ]
        result = cache.get_batch(keys)

        assert result == {
            ("post_99", CounterType.LIKE): 0,
            ("user_99", CounterType.FOLLOWING): 0,
        }

    def test_batch_retrieval_empty_keys_returns_empty_dict(self) -> None:
        cache = CounterCache()
        result = cache.get_batch([])
        assert result == {}

    def test_batch_retrieval_mixed_existing_and_missing(self) -> None:
        cache = CounterCache()
        cache.increment("post_1", CounterType.LIKE)

        keys = [
            ("post_1", CounterType.LIKE),
            ("post_2", CounterType.LIKE),
        ]
        result = cache.get_batch(keys)

        assert result[("post_1", CounterType.LIKE)] == 1
        assert result[("post_2", CounterType.LIKE)] == 0
