"""Unit tests for TokenBucketAlgorithm."""

import time

import pytest

from rate_limiter.algorithms.token_bucket import TokenBucketAlgorithm
from rate_limiter.config import RateLimitRule
from rate_limiter.storage.memory import MemoryStorage


@pytest.fixture
def algorithm():
    """Create a TokenBucketAlgorithm instance."""
    return TokenBucketAlgorithm()


@pytest.fixture
def storage():
    """Create a fresh MemoryStorage instance."""
    return MemoryStorage()


@pytest.fixture
def rule():
    """Create a standard rule: 5 requests per 10 seconds."""
    return RateLimitRule(limit=5, window=10)


class TestAllowWhenTokensAvailable:
    """Tests for allowing requests when tokens are available."""

    def test_first_request_allowed(self, algorithm, storage, rule):
        """First request with a fresh bucket should be allowed."""
        result = algorithm.check("user:1", rule, storage)
        assert result.allowed is True

    def test_first_request_remaining(self, algorithm, storage, rule):
        """First request should show remaining = limit - 1."""
        result = algorithm.check("user:1", rule, storage)
        assert result.remaining == rule.limit - 1

    def test_second_request_allowed(self, algorithm, storage, rule):
        """Second request should still be allowed."""
        algorithm.check("user:1", rule, storage)
        result = algorithm.check("user:1", rule, storage)
        assert result.allowed is True
        assert result.remaining == rule.limit - 2


class TestDenyWhenEmpty:
    """Tests for denying requests when the bucket is empty."""

    def test_deny_after_exhausting_tokens(self, algorithm, storage, rule):
        """After consuming all tokens, the next request should be denied."""
        for _ in range(rule.limit):
            result = algorithm.check("user:1", rule, storage)
            assert result.allowed is True

        result = algorithm.check("user:1", rule, storage)
        assert result.allowed is False

    def test_denied_remaining_is_zero(self, algorithm, storage, rule):
        """Denied request should report remaining = 0."""
        for _ in range(rule.limit):
            algorithm.check("user:1", rule, storage)

        result = algorithm.check("user:1", rule, storage)
        assert result.remaining == 0

    def test_denied_retry_after_positive(self, algorithm, storage, rule):
        """Denied request should have retry_after > 0."""
        for _ in range(rule.limit):
            algorithm.check("user:1", rule, storage)

        result = algorithm.check("user:1", rule, storage)
        assert result.retry_after is not None
        assert result.retry_after > 0


class TestRefillOverTime:
    """Tests for token refill behavior over time."""

    def test_refill_allows_request_after_wait(self, algorithm, storage, rule):
        """After exhausting tokens and waiting, a new request should be allowed."""
        # Exhaust all tokens
        for _ in range(rule.limit):
            algorithm.check("user:1", rule, storage)

        # Refill rate = 5/10 = 0.5 tokens/sec, need 1 token = 2 seconds
        time.sleep(2.1)

        result = algorithm.check("user:1", rule, storage)
        assert result.allowed is True

    def test_partial_refill(self, algorithm, storage, rule):
        """Waiting less than full refill time still refills partial tokens."""
        # Exhaust all tokens
        for _ in range(rule.limit):
            algorithm.check("user:1", rule, storage)

        # Wait enough for ~1 token (2 seconds at 0.5 tokens/sec)
        time.sleep(2.1)

        # Should allow one request
        result = algorithm.check("user:1", rule, storage)
        assert result.allowed is True

        # But not a second one immediately after
        result = algorithm.check("user:1", rule, storage)
        assert result.allowed is False


class TestBurstHandling:
    """Tests for burst capacity (all tokens available at once)."""

    def test_burst_up_to_limit(self, algorithm, storage, rule):
        """All requests up to the limit should be allowed in rapid succession."""
        results = []
        for _ in range(rule.limit):
            results.append(algorithm.check("user:1", rule, storage))

        assert all(r.allowed for r in results)

    def test_burst_remaining_decreases(self, algorithm, storage, rule):
        """Remaining tokens should decrease with each burst request."""
        results = []
        for _ in range(rule.limit):
            results.append(algorithm.check("user:1", rule, storage))

        for i, result in enumerate(results):
            assert result.remaining == rule.limit - 1 - i

    def test_burst_exceeds_limit_denied(self, algorithm, storage, rule):
        """Request beyond burst capacity should be denied."""
        for _ in range(rule.limit):
            algorithm.check("user:1", rule, storage)

        result = algorithm.check("user:1", rule, storage)
        assert result.allowed is False


class TestResultMetadata:
    """Tests for correctness of RateLimitResult metadata fields."""

    def test_limit_field(self, algorithm, storage, rule):
        """Result limit should match the rule's limit."""
        result = algorithm.check("user:1", rule, storage)
        assert result.limit == rule.limit

    def test_remaining_field_on_allow(self, algorithm, storage, rule):
        """Remaining should be limit - consumed tokens when allowed."""
        algorithm.check("user:1", rule, storage)
        algorithm.check("user:1", rule, storage)
        result = algorithm.check("user:1", rule, storage)
        assert result.remaining == rule.limit - 3

    def test_reset_after_field(self, algorithm, storage, rule):
        """reset_after should be a positive float indicating time to full bucket."""
        result = algorithm.check("user:1", rule, storage)
        assert result.reset_after > 0
        assert isinstance(result.reset_after, float)

    def test_retry_after_none_when_allowed(self, algorithm, storage, rule):
        """retry_after should be None when request is allowed."""
        result = algorithm.check("user:1", rule, storage)
        assert result.retry_after is None

    def test_retry_after_set_when_denied(self, algorithm, storage, rule):
        """retry_after should be set to a positive value when denied."""
        for _ in range(rule.limit):
            algorithm.check("user:1", rule, storage)

        result = algorithm.check("user:1", rule, storage)
        assert result.retry_after is not None
        assert result.retry_after > 0

    def test_different_keys_independent(self, algorithm, storage, rule):
        """Different keys should have independent token buckets."""
        # Exhaust tokens for user:1
        for _ in range(rule.limit):
            algorithm.check("user:1", rule, storage)

        # user:2 should still have full bucket
        result = algorithm.check("user:2", rule, storage)
        assert result.allowed is True
        assert result.remaining == rule.limit - 1
