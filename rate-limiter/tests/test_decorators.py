"""Tests for the rate_limit decorator."""

from __future__ import annotations

import pytest

from rate_limiter.config import Algorithm, RateLimitRule, RateLimiterConfig
from rate_limiter.core import RateLimiter
from rate_limiter.decorators import rate_limit, RateLimitExceededException


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def limiter() -> RateLimiter:
    """Create a fresh in-memory RateLimiter for use with the decorator."""
    rule = RateLimitRule(limit=5, window=60, algorithm=Algorithm.TOKEN_BUCKET)
    config = RateLimiterConfig(rules=[rule], storage_backend="memory")
    return RateLimiter(config)


# ---------------------------------------------------------------------------
# 1. Basic function decoration
# ---------------------------------------------------------------------------


class TestBasicDecoration:
    """Test that the decorator preserves function behavior and metadata."""

    def test_decorated_function_returns_correct_value(self, limiter: RateLimiter) -> None:
        """A decorated function should still return its normal result."""

        @rate_limit(limit=5, window=60, limiter=limiter)
        def greet(name: str) -> str:
            return f"Hello, {name}!"

        assert greet("World") == "Hello, World!"

    def test_decorated_function_preserves_name(self, limiter: RateLimiter) -> None:
        """The decorator should preserve the original function's __name__."""

        @rate_limit(limit=5, window=60, limiter=limiter)
        def my_function() -> str:
            return "result"

        assert my_function.__name__ == "my_function"

    def test_decorated_function_preserves_docstring(self, limiter: RateLimiter) -> None:
        """The decorator should preserve the original function's __doc__."""

        @rate_limit(limit=5, window=60, limiter=limiter)
        def documented():
            """This is the docstring."""
            return "ok"

        assert documented.__doc__ == "This is the docstring."

    def test_decorated_function_accepts_args_and_kwargs(self, limiter: RateLimiter) -> None:
        """The decorator should pass through positional and keyword arguments."""

        @rate_limit(limit=5, window=60, limiter=limiter)
        def add(a: int, b: int = 0) -> int:
            return a + b

        assert add(3, b=4) == 7


# ---------------------------------------------------------------------------
# 2. Rate limit enforcement
# ---------------------------------------------------------------------------


class TestRateLimitEnforcement:
    """Test that the decorator enforces rate limits correctly."""

    def test_allows_requests_up_to_limit(self, limiter: RateLimiter) -> None:
        """Requests should be allowed up to the configured limit."""

        @rate_limit(limit=3, window=60, limiter=limiter)
        def action() -> str:
            return "ok"

        # All 3 requests should succeed
        for _ in range(3):
            assert action() == "ok"

    def test_denies_requests_beyond_limit(self, limiter: RateLimiter) -> None:
        """Requests beyond the limit should be denied."""

        @rate_limit(limit=3, window=60, limiter=limiter)
        def action() -> str:
            return "ok"

        # Exhaust the limit
        for _ in range(3):
            action()

        # Next request should raise
        with pytest.raises(RateLimitExceededException):
            action()

    def test_different_functions_share_limiter_but_have_separate_keys(self, limiter: RateLimiter) -> None:
        """Different decorated functions should have independent rate limits."""

        @rate_limit(limit=1, window=60, limiter=limiter)
        def func_a() -> str:
            return "a"

        @rate_limit(limit=1, window=60, limiter=limiter)
        def func_b() -> str:
            return "b"

        # Exhaust func_a
        func_a()
        with pytest.raises(RateLimitExceededException):
            func_a()

        # func_b should still work (different key based on function name)
        assert func_b() == "b"


# ---------------------------------------------------------------------------
# 3. Custom key function
# ---------------------------------------------------------------------------


class TestCustomKeyFunction:
    """Test that custom key functions provide independent rate limits."""

    def test_custom_key_func_isolates_callers(self, limiter: RateLimiter) -> None:
        """Different keys from key_func should get independent rate limits."""

        def user_key(*args, **kwargs) -> str:
            return f"user:{kwargs.get('user_id', 'anonymous')}"

        @rate_limit(limit=2, window=60, key_func=user_key, limiter=limiter)
        def endpoint(user_id: str = "anonymous") -> str:
            return f"data for {user_id}"

        # Exhaust limit for user_1
        endpoint(user_id="user_1")
        endpoint(user_id="user_1")
        with pytest.raises(RateLimitExceededException):
            endpoint(user_id="user_1")

        # user_2 should still be allowed
        assert endpoint(user_id="user_2") == "data for user_2"

    def test_custom_key_func_receives_function_args(self, limiter: RateLimiter) -> None:
        """The key_func should receive the same args/kwargs as the decorated function."""

        received_args = []

        def capture_key(*args, **kwargs) -> str:
            received_args.append((args, kwargs))
            return "fixed-key"

        @rate_limit(limit=5, window=60, key_func=capture_key, limiter=limiter)
        def my_func(x: int, y: str = "hello") -> str:
            return f"{x}-{y}"

        my_func(42, y="world")

        assert len(received_args) == 1
        assert received_args[0] == ((42,), {"y": "world"})

    def test_same_key_shares_limit_across_calls(self, limiter: RateLimiter) -> None:
        """Calls that resolve to the same key should share the rate limit."""

        def constant_key(*args, **kwargs) -> str:
            return "shared-key"

        @rate_limit(limit=2, window=60, key_func=constant_key, limiter=limiter)
        def action(value: int) -> int:
            return value

        assert action(1) == 1
        assert action(2) == 2
        with pytest.raises(RateLimitExceededException):
            action(3)


# ---------------------------------------------------------------------------
# 4. Proper exception on denial
# ---------------------------------------------------------------------------


class TestExceptionOnDenial:
    """Test that RateLimitExceededException is raised with correct attributes."""

    def test_exception_raised_with_correct_limit(self, limiter: RateLimiter) -> None:
        """The exception should contain the configured limit."""

        @rate_limit(limit=2, window=60, limiter=limiter)
        def action() -> str:
            return "ok"

        action()
        action()

        with pytest.raises(RateLimitExceededException) as exc_info:
            action()

        assert exc_info.value.limit == 2

    def test_exception_has_zero_remaining(self, limiter: RateLimiter) -> None:
        """When denied, remaining should be 0."""

        @rate_limit(limit=1, window=60, limiter=limiter)
        def action() -> str:
            return "ok"

        action()

        with pytest.raises(RateLimitExceededException) as exc_info:
            action()

        assert exc_info.value.remaining == 0

    def test_exception_has_positive_reset_after(self, limiter: RateLimiter) -> None:
        """The exception should have a positive reset_after value."""

        @rate_limit(limit=1, window=60, limiter=limiter)
        def action() -> str:
            return "ok"

        action()

        with pytest.raises(RateLimitExceededException) as exc_info:
            action()

        assert exc_info.value.reset_after > 0

    def test_exception_has_retry_after(self, limiter: RateLimiter) -> None:
        """The exception should have a retry_after value when denied."""

        @rate_limit(limit=1, window=60, limiter=limiter)
        def action() -> str:
            return "ok"

        action()

        with pytest.raises(RateLimitExceededException) as exc_info:
            action()

        assert exc_info.value.retry_after is not None
        assert exc_info.value.retry_after > 0

    def test_exception_message_is_descriptive(self, limiter: RateLimiter) -> None:
        """The exception message should describe the rate limit state."""

        @rate_limit(limit=1, window=60, limiter=limiter)
        def action() -> str:
            return "ok"

        action()

        with pytest.raises(RateLimitExceededException) as exc_info:
            action()

        message = str(exc_info.value)
        assert "Rate limit exceeded" in message


# ---------------------------------------------------------------------------
# 5. Non-exception mode (raise_exception=False)
# ---------------------------------------------------------------------------


class TestNonExceptionMode:
    """Test that raise_exception=False returns a dict response on denial."""

    def test_returns_normal_result_when_allowed(self, limiter: RateLimiter) -> None:
        """When under the limit, the function result is returned normally."""

        @rate_limit(limit=5, window=60, raise_exception=False, limiter=limiter)
        def action() -> str:
            return "ok"

        assert action() == "ok"

    def test_returns_dict_with_429_status_on_denial(self, limiter: RateLimiter) -> None:
        """When denied, returns a dict with status_code=429."""

        @rate_limit(limit=1, window=60, raise_exception=False, limiter=limiter)
        def action() -> str:
            return "ok"

        action()  # exhaust limit
        result = action()

        assert isinstance(result, dict)
        assert result["status_code"] == 429

    def test_response_dict_contains_content(self, limiter: RateLimiter) -> None:
        """The 429 response dict should contain error content."""

        @rate_limit(limit=1, window=60, raise_exception=False, limiter=limiter)
        def action() -> str:
            return "ok"

        action()
        result = action()

        assert "content" in result
        assert "error" in result["content"]
        assert "retry_after" in result["content"]

    def test_response_dict_contains_headers(self, limiter: RateLimiter) -> None:
        """The 429 response dict should contain rate limit headers."""

        @rate_limit(limit=1, window=60, raise_exception=False, limiter=limiter)
        def action() -> str:
            return "ok"

        action()
        result = action()

        assert "headers" in result
        headers = result["headers"]
        assert "X-RateLimit-Limit" in headers
        assert "X-RateLimit-Remaining" in headers
        assert "X-RateLimit-Reset" in headers
        assert "Retry-After" in headers


# ---------------------------------------------------------------------------
# 6. Different algorithms work with the decorator
# ---------------------------------------------------------------------------


class TestDifferentAlgorithms:
    """Test that the decorator works with various rate limiting algorithms."""

    @pytest.mark.parametrize("algorithm", [
        "token_bucket",
        "fixed_window",
        "sliding_window_log",
        "sliding_window_counter",
    ])
    def test_algorithm_allows_then_denies(self, algorithm: str, limiter: RateLimiter) -> None:
        """Each algorithm should allow requests up to the limit, then deny."""

        @rate_limit(limit=2, window=60, algorithm=algorithm, limiter=limiter)
        def action() -> str:
            return "ok"

        # Should be allowed
        assert action() == "ok"
        assert action() == "ok"

        # Should be denied
        with pytest.raises(RateLimitExceededException):
            action()

    def test_leaking_bucket_enforces_limit(self, limiter: RateLimiter) -> None:
        """Leaking bucket should deny when the queue is full.

        The leaking bucket drains continuously at (limit/window) per second.
        With limit=100 and window=1, drain_rate=100/s. Rapid calls fill the
        queue faster than it drains, so eventually requests are denied.
        """

        @rate_limit(limit=100, window=1, algorithm="leaking_bucket", limiter=limiter)
        def action() -> str:
            return "ok"

        # Rapidly fill the queue — at some point it must deny
        denied = False
        for _ in range(200):
            try:
                action()
            except RateLimitExceededException:
                denied = True
                break

        assert denied, "Leaking bucket should eventually deny requests"

    def test_invalid_algorithm_raises_value_error(self) -> None:
        """An invalid algorithm name should raise ValueError at decoration time."""
        with pytest.raises(ValueError, match="Unknown algorithm"):
            @rate_limit(limit=5, window=60, algorithm="nonexistent")
            def action() -> str:
                return "ok"
