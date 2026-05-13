"""Tests for Flask rate limiting middleware/extension."""

from __future__ import annotations

import pytest
from flask import Flask

from rate_limiter.config import Algorithm, RateLimitRule, RateLimiterConfig
from rate_limiter.core import RateLimiter
from rate_limiter.middleware.flask import RateLimitExtension


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_app(
    rules: list[RateLimitRule],
    auto_headers: bool = True,
    init_app_pattern: bool = False,
) -> Flask:
    """Create a Flask app with rate limiting extension configured.

    Args:
        rules: Rate limit rules to apply.
        auto_headers: Whether to add rate limit headers to allowed responses.
        init_app_pattern: If True, use the init_app pattern instead of passing app directly.
    """
    app = Flask(__name__)
    app.config["TESTING"] = True

    config = RateLimiterConfig(rules=rules, storage_backend="memory")
    limiter = RateLimiter(config)

    if init_app_pattern:
        ext = RateLimitExtension(limiter=limiter, auto_headers=auto_headers)
        ext.init_app(app)
    else:
        RateLimitExtension(app=app, limiter=limiter, auto_headers=auto_headers)

    @app.route("/hello")
    def hello():
        return {"message": "hello"}

    @app.route("/world")
    def world():
        return {"message": "world"}

    return app


# ---------------------------------------------------------------------------
# 1. Allowed requests get rate limit headers
# ---------------------------------------------------------------------------


class TestAllowedRequestsGetHeaders:
    """Allowed responses include X-RateLimit-* headers."""

    def test_response_has_rate_limit_headers(self) -> None:
        """A successful request should include all rate limit headers."""
        rule = RateLimitRule(limit=10, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule])
        client = app.test_client()

        response = client.get("/hello")

        assert response.status_code == 200
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers
        assert "X-RateLimit-Reset" in response.headers

    def test_rate_limit_header_values_are_correct(self) -> None:
        """Header values should reflect the rule's limit and remaining count."""
        rule = RateLimitRule(limit=5, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule])
        client = app.test_client()

        response = client.get("/hello")

        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == "5"
        # After one request, remaining should be limit - 1
        assert response.headers["X-RateLimit-Remaining"] == "4"

    def test_remaining_decreases_with_each_request(self) -> None:
        """Each allowed request should decrement the remaining count."""
        rule = RateLimitRule(limit=5, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule])
        client = app.test_client()

        r1 = client.get("/hello")
        r2 = client.get("/hello")
        r3 = client.get("/hello")

        assert int(r1.headers["X-RateLimit-Remaining"]) == 4
        assert int(r2.headers["X-RateLimit-Remaining"]) == 3
        assert int(r3.headers["X-RateLimit-Remaining"]) == 2


# ---------------------------------------------------------------------------
# 2. Denied requests get 429 with proper body and headers
# ---------------------------------------------------------------------------


class TestDeniedRequestsGet429:
    """Requests exceeding the rate limit receive 429 responses."""

    def test_returns_429_when_limit_exceeded(self) -> None:
        """After exhausting the limit, the next request gets 429."""
        rule = RateLimitRule(limit=2, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule])
        client = app.test_client()

        # Exhaust the limit
        client.get("/hello")
        client.get("/hello")

        # Next request should be denied
        response = client.get("/hello")
        assert response.status_code == 429

    def test_429_response_has_json_body(self) -> None:
        """429 response should include a JSON error body."""
        rule = RateLimitRule(limit=1, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule])
        client = app.test_client()

        client.get("/hello")
        response = client.get("/hello")

        assert response.status_code == 429
        body = response.get_json()
        assert "error" in body
        assert body["error"] == "Too Many Requests"
        assert "retry_after" in body

    def test_429_response_has_rate_limit_headers(self) -> None:
        """429 response should include X-RateLimit-* and Retry-After headers."""
        rule = RateLimitRule(limit=1, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule])
        client = app.test_client()

        client.get("/hello")
        response = client.get("/hello")

        assert response.status_code == 429
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers
        assert "X-RateLimit-Reset" in response.headers
        assert "Retry-After" in response.headers

    def test_429_headers_have_correct_values(self) -> None:
        """429 headers should reflect the exhausted state."""
        rule = RateLimitRule(limit=1, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule])
        client = app.test_client()

        client.get("/hello")
        response = client.get("/hello")

        assert response.status_code == 429
        assert response.headers["X-RateLimit-Limit"] == "1"
        assert response.headers["X-RateLimit-Remaining"] == "0"
        # Retry-After should be a positive number
        retry_after = float(response.headers["Retry-After"])
        assert retry_after > 0


# ---------------------------------------------------------------------------
# 3. Multiple rules applied (most restrictive wins)
# ---------------------------------------------------------------------------


class TestMultipleRulesApplied:
    """When multiple rules are configured, the most restrictive one wins."""

    def test_strict_rule_denies_before_lenient_rule(self) -> None:
        """A strict rule should deny even if a lenient rule would allow."""
        strict_rule = RateLimitRule(limit=2, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        lenient_rule = RateLimitRule(limit=100, window=3600, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([strict_rule, lenient_rule])
        client = app.test_client()

        # First two requests allowed
        r1 = client.get("/hello")
        r2 = client.get("/hello")
        assert r1.status_code == 200
        assert r2.status_code == 200

        # Third request denied by strict rule
        r3 = client.get("/hello")
        assert r3.status_code == 429

    def test_lowest_remaining_shown_when_all_allowed(self) -> None:
        """When all rules allow, the response shows the lowest remaining count."""
        rule_a = RateLimitRule(limit=3, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        rule_b = RateLimitRule(limit=100, window=3600, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule_a, rule_b])
        client = app.test_client()

        response = client.get("/hello")

        assert response.status_code == 200
        # The most restrictive remaining should be from rule_a (limit 3 - 1 = 2)
        assert int(response.headers["X-RateLimit-Remaining"]) == 2


# ---------------------------------------------------------------------------
# 4. Different IPs get independent rate limits
# ---------------------------------------------------------------------------


class TestIndependentRateLimits:
    """Different clients get independent rate limit counters."""

    def test_different_ips_have_independent_limits(self) -> None:
        """Requests from different IPs should have separate rate limit counters."""
        rule = RateLimitRule(limit=1, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule])
        client = app.test_client()

        # First IP exhausts its limit
        r1 = client.get("/hello", headers={"X-Forwarded-For": "10.0.0.1"})
        assert r1.status_code == 200

        r2 = client.get("/hello", headers={"X-Forwarded-For": "10.0.0.1"})
        assert r2.status_code == 429

        # Second IP should still be allowed
        r3 = client.get("/hello", headers={"X-Forwarded-For": "10.0.0.2"})
        assert r3.status_code == 200

    def test_remote_addr_used_when_no_forwarded_for(self) -> None:
        """When no X-Forwarded-For header, REMOTE_ADDR is used for key resolution."""
        rule = RateLimitRule(limit=1, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule])
        client = app.test_client()

        # Default test client uses 127.0.0.1 as REMOTE_ADDR
        r1 = client.get("/hello")
        assert r1.status_code == 200

        r2 = client.get("/hello")
        assert r2.status_code == 429


# ---------------------------------------------------------------------------
# 5. RateLimitExtension init_app pattern works
# ---------------------------------------------------------------------------


class TestInitAppPattern:
    """The init_app pattern for deferred initialization works correctly."""

    def test_init_app_registers_hooks(self) -> None:
        """Using init_app should register before/after request hooks."""
        rule = RateLimitRule(limit=5, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule], init_app_pattern=True)
        client = app.test_client()

        response = client.get("/hello")

        assert response.status_code == 200
        assert "X-RateLimit-Limit" in response.headers
        assert response.headers["X-RateLimit-Limit"] == "5"

    def test_init_app_enforces_rate_limits(self) -> None:
        """Rate limits should be enforced when using init_app pattern."""
        rule = RateLimitRule(limit=1, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule], init_app_pattern=True)
        client = app.test_client()

        r1 = client.get("/hello")
        assert r1.status_code == 200

        r2 = client.get("/hello")
        assert r2.status_code == 429

    def test_extension_stored_on_app(self) -> None:
        """The extension instance should be stored on app.extensions."""
        rule = RateLimitRule(limit=5, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule], init_app_pattern=True)

        assert "rate_limiter" in app.extensions
        assert isinstance(app.extensions["rate_limiter"], RateLimitExtension)


# ---------------------------------------------------------------------------
# 6. auto_headers=False disables headers on allowed responses
# ---------------------------------------------------------------------------


class TestAutoHeadersDisabled:
    """When auto_headers=False, rate limit headers are not added to allowed responses."""

    def test_no_headers_on_allowed_response(self) -> None:
        """Allowed responses should not have rate limit headers when auto_headers=False."""
        rule = RateLimitRule(limit=10, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule], auto_headers=False)
        client = app.test_client()

        response = client.get("/hello")

        assert response.status_code == 200
        assert "X-RateLimit-Limit" not in response.headers
        assert "X-RateLimit-Remaining" not in response.headers
        assert "X-RateLimit-Reset" not in response.headers

    def test_429_still_has_headers_when_auto_headers_false(self) -> None:
        """Denied responses should still include rate limit headers even with auto_headers=False."""
        rule = RateLimitRule(limit=1, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule], auto_headers=False)
        client = app.test_client()

        client.get("/hello")
        response = client.get("/hello")

        assert response.status_code == 429
        assert "X-RateLimit-Limit" in response.headers
        assert "Retry-After" in response.headers
