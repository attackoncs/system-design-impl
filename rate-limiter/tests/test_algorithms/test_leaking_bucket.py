"""Unit tests for LeakingBucketAlgorithm."""

import time

import pytest

from rate_limiter.algorithms.leaking_bucket import LeakingBucketAlgorithm
from rate_limiter.config import RateLimitRule
from rate_limiter.storage.memory import MemoryStorage


@pytest.fixture
def algorithm():
    """Create a LeakingBucketAlgorithm instance."""
    return LeakingBucketAlgorithm()


@pytest.fixture
def storage():
    """Create a fresh MemoryStorage instance."""
    return MemoryStorage()


@pytest.fixture
def rule():
    """Create a standard rule: capacity=5, drain_rate=0.5/sec (5 requests per 10 seconds)."""
    return RateLimitRule(limit=5, window=10)


def _fill_queue(algorithm, storage, rule, key="user:1"):
    """Fill the queue until a request is denied, returning the denied result.

    Because the leaking bucket drains continuously (even between rapid calls),
    we keep sending requests until one is actually denied.
    """
    for _ in range(rule.limit + 5):
        result = algorithm.check(key, rule, storage)
        if not result.allowed:
            return result
    return None


class TestAllowWhenQueueHasSpace:
    """Tests for allowing requests when the queue has space."""

    def test_first_request_allowed(self, algorithm, storage, rule):
        """First request with an empty queue should be allowed."""
        result = algorithm.check("user:1", rule, storage)
        assert result.allowed is True

    def test_first_request_remaining(self, algorithm, storage, rule):
        """First request should show remaining = capacity - 1."""
        result = algorithm.check("user:1", rule, storage)
        assert result.remaining == rule.limit - 1


class TestAllowMultipleRequests:
    """Tests for allowing multiple requests up to capacity."""

    def test_all_requests_up_to_capacity_allowed(self, algorithm, storage, rule):
        """All requests up to the capacity should be allowed."""
        results = []
        for _ in range(rule.limit):
            results.append(algorithm.check("user:1", rule, storage))

        assert all(r.allowed for r in results)

    def test_last_request_at_capacity_has_zero_remaining(self, algorithm, storage, rule):
        """The last allowed request should report remaining = 0."""
        for _ in range(rule.limit - 1):
            algorithm.check("user:1", rule, storage)

        result = algorithm.check("user:1", rule, storage)
        assert result.allowed is True
        assert result.remaining == 0


class TestDenyWhenFull:
    """Tests for denying requests when the queue is full."""

    def test_deny_after_filling_queue(self, algorithm, storage, rule):
        """After filling the queue to capacity, a request should be denied."""
        result = _fill_queue(algorithm, storage, rule)
        assert result is not None
        assert result.allowed is False

    def test_denied_remaining_is_zero(self, algorithm, storage, rule):
        """Denied request should report remaining = 0."""
        result = _fill_queue(algorithm, storage, rule)
        assert result is not None
        assert result.remaining == 0


class TestDeniedRetryAfter:
    """Tests for retry_after on denied requests."""

    def test_denied_retry_after_positive(self, algorithm, storage, rule):
        """Denied request should have retry_after > 0."""
        result = _fill_queue(algorithm, storage, rule)
        assert result is not None
        assert result.retry_after is not None
        assert result.retry_after > 0

    def test_retry_after_none_when_allowed(self, algorithm, storage, rule):
        """Allowed request should have retry_after = None."""
        result = algorithm.check("user:1", rule, storage)
        assert result.retry_after is None


class TestDrainOverTime:
    """Tests for queue draining behavior over time."""

    def test_drain_allows_request_after_wait(self, algorithm, storage, rule):
        """After filling the queue and waiting for drain, a new request should be allowed."""
        # Fill the queue until denied
        result = _fill_queue(algorithm, storage, rule)
        assert result is not None
        assert result.allowed is False

        # Drain rate = 5/10 = 0.5 items/sec, need 1 item drained = 2 seconds
        time.sleep(2.1)

        result = algorithm.check("user:1", rule, storage)
        assert result.allowed is True

    def test_still_denied_before_drain(self, algorithm, storage, rule):
        """Request should still be denied if not enough time has passed for drain."""
        # Fill the queue until denied
        result = _fill_queue(algorithm, storage, rule)
        assert result is not None

        # Drain rate = 0.5/sec, need 2 seconds for 1 item. Wait much less than that.
        time.sleep(0.1)

        result = algorithm.check("user:1", rule, storage)
        assert result.allowed is False


class TestRemainingDecreases:
    """Tests for remaining count decreasing with each request."""

    def test_remaining_decreases_with_each_request(self, algorithm, storage, rule):
        """Remaining should decrease by 1 with each request."""
        results = []
        for _ in range(rule.limit):
            results.append(algorithm.check("user:1", rule, storage))

        for i, result in enumerate(results):
            assert result.remaining == rule.limit - 1 - i


class TestDifferentKeysIndependent:
    """Tests for independent queues per key."""

    def test_different_keys_have_independent_queues(self, algorithm, storage, rule):
        """Different keys should have independent queues."""
        # Fill the queue for user:1 until denied
        result = _fill_queue(algorithm, storage, rule, key="user:1")
        assert result is not None
        assert result.allowed is False

        # user:2 should still have full capacity
        result = algorithm.check("user:2", rule, storage)
        assert result.allowed is True
        assert result.remaining == rule.limit - 1
