"""Tests for the core RateLimiter orchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from rate_limiter.algorithms.base import RateLimitResult
from rate_limiter.config import Algorithm, RateLimitRule, RateLimiterConfig
from rate_limiter.core import RateLimiter
from rate_limiter.storage.memory import MemoryStorage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def basic_rule() -> RateLimitRule:
    """A simple token bucket rule: 5 requests per 60 seconds."""
    return RateLimitRule(limit=5, window=60, algorithm=Algorithm.TOKEN_BUCKET)


@pytest.fixture
def memory_config(basic_rule: RateLimitRule) -> RateLimiterConfig:
    """Config using memory backend with a single rule."""
    return RateLimiterConfig(
        rules=[basic_rule],
        storage_backend="memory",
        fail_open=True,
    )


@pytest.fixture
def limiter(memory_config: RateLimiterConfig) -> RateLimiter:
    """RateLimiter instance with memory backend."""
    return RateLimiter(memory_config)


# ---------------------------------------------------------------------------
# 1. Single rule check (allowed and denied)
# ---------------------------------------------------------------------------


class TestSingleRuleCheck:
    """Test check() with a single rule — allowed and denied scenarios."""

    def test_check_allowed_when_under_limit(self, limiter: RateLimiter, basic_rule: RateLimitRule) -> None:
        """First request should be allowed with remaining tokens."""
        result = limiter.check("user:1", basic_rule)
        assert result.allowed is True
        assert result.remaining >= 0
        assert result.limit == basic_rule.limit

    def test_check_denied_when_limit_exhausted(self, limiter: RateLimiter, basic_rule: RateLimitRule) -> None:
        """After exhausting the limit, requests should be denied."""
        # Exhaust all tokens
        for _ in range(basic_rule.limit):
            limiter.check("user:2", basic_rule)

        # Next request should be denied
        result = limiter.check("user:2", basic_rule)
        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after is not None
        assert result.retry_after > 0


# ---------------------------------------------------------------------------
# 2. Multiple rules — most restrictive wins
# ---------------------------------------------------------------------------


class TestMultipleRulesMostRestrictive:
    """Test check_request() returns the most restrictive result."""

    def test_denied_result_preferred_over_allowed(self) -> None:
        """If one rule denies, the overall result should be denied."""
        # Strict rule: 1 request per 60s
        strict_rule = RateLimitRule(limit=1, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        # Lenient rule: 100 requests per 60s
        lenient_rule = RateLimitRule(limit=100, window=60, algorithm=Algorithm.TOKEN_BUCKET)

        config = RateLimiterConfig(
            rules=[strict_rule, lenient_rule],
            storage_backend="memory",
            fail_open=True,
        )
        limiter = RateLimiter(config)

        # First request allowed by both
        result = limiter.check_request("client-a")
        assert result.allowed is True

        # Second request should be denied by strict rule
        result = limiter.check_request("client-a")
        assert result.allowed is False

    def test_lowest_remaining_when_all_allowed(self) -> None:
        """If all rules allow, the result with lowest remaining is returned."""
        # Rule A: 3 requests per 60s
        rule_a = RateLimitRule(limit=3, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        # Rule B: 10 requests per 60s
        rule_b = RateLimitRule(limit=10, window=60, algorithm=Algorithm.TOKEN_BUCKET)

        config = RateLimiterConfig(
            rules=[rule_a, rule_b],
            storage_backend="memory",
            fail_open=True,
        )
        limiter = RateLimiter(config)

        # First request — rule_a has 2 remaining, rule_b has 9 remaining
        result = limiter.check_request("client-b")
        assert result.allowed is True
        # Most restrictive remaining should be from rule_a
        assert result.remaining == 2


# ---------------------------------------------------------------------------
# 3. Dynamic rule update via update_rules()
# ---------------------------------------------------------------------------


class TestDynamicRuleUpdate:
    """Test update_rules() dynamically changes active rules."""

    def test_update_rules_replaces_existing(self, limiter: RateLimiter) -> None:
        """update_rules() should replace the config's rules list."""
        new_rule = RateLimitRule(limit=1000, window=3600, algorithm=Algorithm.FIXED_WINDOW)
        limiter.update_rules([new_rule])

        assert limiter.config.rules == [new_rule]
        assert len(limiter.config.rules) == 1

    def test_updated_rules_are_used_in_check_request(self) -> None:
        """After update_rules(), check_request uses the new rules."""
        initial_rule = RateLimitRule(limit=1, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        config = RateLimiterConfig(
            rules=[initial_rule],
            storage_backend="memory",
        )
        limiter = RateLimiter(config)

        # Exhaust initial rule
        limiter.check_request("client-c")
        result = limiter.check_request("client-c")
        assert result.allowed is False

        # Update to a more lenient rule
        new_rule = RateLimitRule(limit=100, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        limiter.update_rules([new_rule])

        # Now requests should be allowed again (new rule, fresh key due to different params)
        result = limiter.check_request("client-c")
        assert result.allowed is True


# ---------------------------------------------------------------------------
# 4. Storage backend selection (memory vs redis — mock redis)
# ---------------------------------------------------------------------------


class TestStorageBackendSelection:
    """Test that the correct storage backend is created based on config."""

    def test_memory_backend_selected(self) -> None:
        """Config with storage_backend='memory' creates MemoryStorage."""
        config = RateLimiterConfig(storage_backend="memory")
        limiter = RateLimiter(config)
        assert isinstance(limiter.storage, MemoryStorage)

    @patch("rate_limiter.core.RedisStorage")
    def test_redis_backend_selected(self, mock_redis_cls: MagicMock) -> None:
        """Config with storage_backend='redis' creates RedisStorage."""
        mock_redis_cls.return_value = MagicMock()
        config = RateLimiterConfig(
            storage_backend="redis",
            redis_url="redis://localhost:6379",
        )
        limiter = RateLimiter(config)
        mock_redis_cls.assert_called_once_with("redis://localhost:6379")

    def test_invalid_backend_raises_value_error(self) -> None:
        """Config with unknown storage_backend raises ValueError."""
        config = RateLimiterConfig(storage_backend="unknown")
        with pytest.raises(ValueError, match="Unsupported storage backend"):
            RateLimiter(config)


# ---------------------------------------------------------------------------
# 5. Fail-open behavior (allow on storage error)
# ---------------------------------------------------------------------------


class TestFailOpenBehavior:
    """Test that fail_open=True allows requests when storage errors occur."""

    def test_fail_open_allows_on_storage_error(self) -> None:
        """When storage raises an exception and fail_open=True, request is allowed."""
        rule = RateLimitRule(limit=10, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        config = RateLimiterConfig(
            rules=[rule],
            storage_backend="memory",
            fail_open=True,
        )
        limiter = RateLimiter(config)

        # Make storage raise an exception
        limiter._storage = MagicMock()
        limiter._storage.execute_atomic.side_effect = RuntimeError("Storage unavailable")
        limiter._storage.get.side_effect = RuntimeError("Storage unavailable")

        result = limiter.check("test-key", rule)
        assert result.allowed is True
        assert result.remaining == rule.limit
        assert result.retry_after is None


# ---------------------------------------------------------------------------
# 6. Fail-closed behavior (deny on storage error)
# ---------------------------------------------------------------------------


class TestFailClosedBehavior:
    """Test that fail_open=False denies requests when storage errors occur."""

    def test_fail_closed_denies_on_storage_error(self) -> None:
        """When storage raises an exception and fail_open=False, request is denied."""
        rule = RateLimitRule(limit=10, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        config = RateLimiterConfig(
            rules=[rule],
            storage_backend="memory",
            fail_open=False,
        )
        limiter = RateLimiter(config)

        # Make storage raise an exception
        limiter._storage = MagicMock()
        limiter._storage.execute_atomic.side_effect = RuntimeError("Storage unavailable")
        limiter._storage.get.side_effect = RuntimeError("Storage unavailable")

        result = limiter.check("test-key", rule)
        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after == float(rule.window)


# ---------------------------------------------------------------------------
# 7. Plain string key support
# ---------------------------------------------------------------------------


class TestPlainStringKeySupport:
    """Test that check_request() accepts a plain string as the key directly."""

    def test_plain_string_used_as_key(self) -> None:
        """When request_context is a string, it's used directly as the key."""
        rule = RateLimitRule(limit=5, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        config = RateLimiterConfig(
            rules=[rule],
            storage_backend="memory",
        )
        limiter = RateLimiter(config)

        # Use a plain string as the request context
        result = limiter.check_request("my-custom-key")
        assert result.allowed is True
        assert result.remaining == 4

    def test_different_string_keys_are_independent(self) -> None:
        """Different string keys should have independent rate limits."""
        rule = RateLimitRule(limit=1, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        config = RateLimiterConfig(
            rules=[rule],
            storage_backend="memory",
        )
        limiter = RateLimiter(config)

        # Exhaust key A
        limiter.check_request("key-a")
        result_a = limiter.check_request("key-a")
        assert result_a.allowed is False

        # Key B should still be allowed
        result_b = limiter.check_request("key-b")
        assert result_b.allowed is True


# ---------------------------------------------------------------------------
# 8. Request object key resolution (key_func and ip_key fallback)
# ---------------------------------------------------------------------------


class TestRequestObjectKeyResolution:
    """Test key resolution from request objects using key_func or ip_key fallback."""

    def test_custom_key_func_used_when_provided(self) -> None:
        """When rule has key_func, it's used to resolve the key from request."""
        custom_key_func = lambda req: f"user:{req.user_id}"

        rule = RateLimitRule(
            limit=5,
            window=60,
            algorithm=Algorithm.TOKEN_BUCKET,
            key_func=custom_key_func,
        )
        config = RateLimiterConfig(
            rules=[rule],
            storage_backend="memory",
        )
        limiter = RateLimiter(config)

        # Create a mock request object
        request = MagicMock()
        request.user_id = "42"

        result = limiter.check_request(request)
        assert result.allowed is True

    def test_ip_key_fallback_when_no_key_func(self) -> None:
        """When no key_func is provided, ip_key is used as fallback."""
        rule = RateLimitRule(
            limit=5,
            window=60,
            algorithm=Algorithm.TOKEN_BUCKET,
        )
        config = RateLimiterConfig(
            rules=[rule],
            storage_backend="memory",
        )
        limiter = RateLimiter(config)

        # Create a mock request with headers dict-like object
        request = MagicMock()
        headers = MagicMock()
        headers.get = lambda key: {"X-Forwarded-For": "192.168.1.100", "x-forwarded-for": "192.168.1.100"}.get(key)
        request.headers = headers

        result = limiter.check_request(request)
        assert result.allowed is True

    def test_different_request_objects_get_different_keys(self) -> None:
        """Different request objects should resolve to different keys."""
        custom_key_func = lambda req: req.api_key

        rule = RateLimitRule(
            limit=1,
            window=60,
            algorithm=Algorithm.TOKEN_BUCKET,
            key_func=custom_key_func,
        )
        config = RateLimiterConfig(
            rules=[rule],
            storage_backend="memory",
        )
        limiter = RateLimiter(config)

        # Exhaust limit for request A
        req_a = MagicMock()
        req_a.api_key = "key-alpha"
        limiter.check_request(req_a)
        result_a = limiter.check_request(req_a)
        assert result_a.allowed is False

        # Request B with different key should still be allowed
        req_b = MagicMock()
        req_b.api_key = "key-beta"
        result_b = limiter.check_request(req_b)
        assert result_b.allowed is True
