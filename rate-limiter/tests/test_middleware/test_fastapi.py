"""Tests for FastAPI rate limiting middleware."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from rate_limiter.config import Algorithm, RateLimitRule, RateLimiterConfig
from rate_limiter.core import RateLimiter
from rate_limiter.middleware.fastapi import RateLimitConfig, RateLimitMiddleware


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_app(rules: list[RateLimitRule], auto_headers: bool = True) -> FastAPI:
    """Create a FastAPI app with rate limiting middleware configured."""
    app = FastAPI()

    config = RateLimiterConfig(rules=rules, storage_backend="memory")
    limiter = RateLimiter(config)

    app.add_middleware(
        RateLimitMiddleware,
        limiter=limiter,
        auto_headers=auto_headers,
    )

    @app.get("/hello")
    def hello():
        return {"message": "hello"}

    @app.get("/world")
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
        client = TestClient(app)

        response = client.get("/hello")

        assert response.status_code == 200
        assert "x-ratelimit-limit" in response.headers
        assert "x-ratelimit-remaining" in response.headers
        assert "x-ratelimit-reset" in response.headers

    def test_rate_limit_header_values_are_correct(self) -> None:
        """Header values should reflect the rule's limit and remaining count."""
        rule = RateLimitRule(limit=5, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule])
        client = TestClient(app)

        response = client.get("/hello")

        assert response.status_code == 200
        assert response.headers["x-ratelimit-limit"] == "5"
        # After one request, remaining should be limit - 1
        assert response.headers["x-ratelimit-remaining"] == "4"

    def test_remaining_decreases_with_each_request(self) -> None:
        """Each allowed request should decrement the remaining count."""
        rule = RateLimitRule(limit=5, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule])
        client = TestClient(app)

        r1 = client.get("/hello")
        r2 = client.get("/hello")
        r3 = client.get("/hello")

        assert int(r1.headers["x-ratelimit-remaining"]) == 4
        assert int(r2.headers["x-ratelimit-remaining"]) == 3
        assert int(r3.headers["x-ratelimit-remaining"]) == 2

    def test_no_headers_when_auto_headers_disabled(self) -> None:
        """When auto_headers=False, rate limit headers are not added to allowed responses."""
        rule = RateLimitRule(limit=10, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule], auto_headers=False)
        client = TestClient(app)

        response = client.get("/hello")

        assert response.status_code == 200
        assert "x-ratelimit-limit" not in response.headers
        assert "x-ratelimit-remaining" not in response.headers
        assert "x-ratelimit-reset" not in response.headers


# ---------------------------------------------------------------------------
# 2. Denied requests get 429 with proper body and headers
# ---------------------------------------------------------------------------


class TestDeniedRequestsGet429:
    """Requests exceeding the rate limit receive 429 responses."""

    def test_returns_429_when_limit_exceeded(self) -> None:
        """After exhausting the limit, the next request gets 429."""
        rule = RateLimitRule(limit=2, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule])
        client = TestClient(app)

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
        client = TestClient(app)

        client.get("/hello")
        response = client.get("/hello")

        assert response.status_code == 429
        body = response.json()
        assert "error" in body
        assert body["error"] == "Too Many Requests"
        assert "retry_after" in body

    def test_429_response_has_rate_limit_headers(self) -> None:
        """429 response should include X-RateLimit-* and Retry-After headers."""
        rule = RateLimitRule(limit=1, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule])
        client = TestClient(app)

        client.get("/hello")
        response = client.get("/hello")

        assert response.status_code == 429
        assert "x-ratelimit-limit" in response.headers
        assert "x-ratelimit-remaining" in response.headers
        assert "x-ratelimit-reset" in response.headers
        assert "retry-after" in response.headers

    def test_429_headers_have_correct_values(self) -> None:
        """429 headers should reflect the exhausted state."""
        rule = RateLimitRule(limit=1, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule])
        client = TestClient(app)

        client.get("/hello")
        response = client.get("/hello")

        assert response.status_code == 429
        assert response.headers["x-ratelimit-limit"] == "1"
        assert response.headers["x-ratelimit-remaining"] == "0"
        # Retry-After should be a positive number
        retry_after = float(response.headers["retry-after"])
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
        client = TestClient(app)

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
        client = TestClient(app)

        response = client.get("/hello")

        assert response.status_code == 200
        # The most restrictive remaining should be from rule_a (limit 3 - 1 = 2)
        assert int(response.headers["x-ratelimit-remaining"]) == 2


# ---------------------------------------------------------------------------
# 4. Different endpoints/IPs get independent rate limits
# ---------------------------------------------------------------------------


class TestIndependentRateLimits:
    """Different clients/endpoints get independent rate limit counters."""

    def test_different_ips_have_independent_limits(self) -> None:
        """Requests from different IPs should have separate rate limit counters."""
        rule = RateLimitRule(limit=1, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        app = _create_app([rule])
        client = TestClient(app)

        # First IP exhausts its limit
        r1 = client.get("/hello", headers={"X-Forwarded-For": "10.0.0.1"})
        assert r1.status_code == 200

        r2 = client.get("/hello", headers={"X-Forwarded-For": "10.0.0.1"})
        assert r2.status_code == 429

        # Second IP should still be allowed
        r3 = client.get("/hello", headers={"X-Forwarded-For": "10.0.0.2"})
        assert r3.status_code == 200


# ---------------------------------------------------------------------------
# 5. RateLimitConfig helper class
# ---------------------------------------------------------------------------


class TestRateLimitConfigHelper:
    """Test the RateLimitConfig convenience class."""

    def test_add_rule_and_get_config(self) -> None:
        """RateLimitConfig.add_rule() creates rules and get_config() returns config."""
        rate_config = RateLimitConfig()
        rate_config.add_rule(limit=100, window=60, key_type="ip")

        config = rate_config.get_config()
        assert len(config.rules) == 1
        assert config.rules[0].limit == 100
        assert config.rules[0].window == 60

    def test_middleware_works_with_rate_limit_config(self) -> None:
        """RateLimitConfig integrates correctly with the middleware."""
        app = FastAPI()

        rate_config = RateLimitConfig()
        rate_config.add_rule(limit=2, window=60, algorithm="token_bucket", key_type="ip")

        app.add_middleware(
            RateLimitMiddleware,
            config=rate_config.get_config(),
        )

        @app.get("/test")
        def test_endpoint():
            return {"ok": True}

        client = TestClient(app)

        r1 = client.get("/test")
        r2 = client.get("/test")
        assert r1.status_code == 200
        assert r2.status_code == 200

        r3 = client.get("/test")
        assert r3.status_code == 429
