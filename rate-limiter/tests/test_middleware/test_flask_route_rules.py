"""Route policies stay explicit when Flask decorators share a limiter."""

import flask
import pytest
from rate_limiter import RateLimiter, RateLimiterConfig, RateLimitRule
from rate_limiter.middleware.flask import rate_limit
from rate_limiter.storage.memory import MemoryStorage


@pytest.mark.parametrize("configured_rules", [[], [RateLimitRule(100, 3600)]])
def test_supplied_limiter_enforces_the_decorated_rule(configured_rules):
    app = flask.Flask(__name__)
    limiter = RateLimiter(
        RateLimiterConfig(rules=configured_rules, fail_open=False),
        storage=MemoryStorage(clock=lambda: 120),
    )

    @app.get("/limited")
    @rate_limit(1, 60, algorithm="fixed_window", limiter=limiter)
    def endpoint():
        return "ok"

    client = app.test_client()
    assert client.get("/limited").status_code == 200
    assert client.get("/limited").status_code == 429
    assert limiter.config.rules == configured_rules
    assert limiter.metrics()["storage_errors"] == 0


def test_supplied_limiter_honors_the_route_algorithm_and_window():
    now = [150.0]
    app = flask.Flask(__name__)
    limiter = RateLimiter(
        RateLimiterConfig(), storage=MemoryStorage(clock=lambda: now[0])
    )

    @app.get("/limited")
    @rate_limit(1, 60, algorithm="fixed_window", limiter=limiter)
    def endpoint():
        return "ok"

    client = app.test_client()
    assert client.get("/limited").status_code == 200
    now[0] = 179.0
    assert client.get("/limited").status_code == 429
    now[0] = 180.0
    assert client.get("/limited").status_code == 200


def test_shared_limiter_keeps_distinct_route_quotas():
    app = flask.Flask(__name__)
    limiter = RateLimiter(
        RateLimiterConfig(rules=[RateLimitRule(1, 60)]),
        storage=MemoryStorage(clock=lambda: 120),
    )
    decorate = rate_limit(1, 60, limiter=limiter)

    @app.get("/first")
    @decorate
    def first():
        return "first"

    @app.get("/second")
    @decorate
    def second():
        return "second"

    client = app.test_client()
    assert client.get("/first").status_code == 200
    assert client.get("/second").status_code == 200
    assert client.get("/first").status_code == 429
    assert client.get("/second").status_code == 429


@pytest.mark.parametrize("endpoint_prefix", ["", "long" * 100])
def test_factory_views_are_isolated_by_registered_endpoint(endpoint_prefix):
    app = flask.Flask(__name__)
    limiter = RateLimiter(RateLimiterConfig())

    def make_view():
        @rate_limit(1, 60, limiter=limiter)
        def view():
            return "ok"

        return view

    for name in ("first", "second"):
        app.add_url_rule(
            f"/{name}", endpoint=endpoint_prefix + name, view_func=make_view()
        )
    client = app.test_client()
    assert client.get("/first").status_code == 200
    assert client.get("/second").status_code == 200
    assert client.get("/first").status_code == 429
    assert client.get("/second").status_code == 429


@pytest.mark.parametrize(
    "key_type,first_request,second_request",
    [
        (
            "ip",
            {"path": "/one", "environ_overrides": {"REMOTE_ADDR": "10.0.0.1"}},
            {"path": "/one", "environ_overrides": {"REMOTE_ADDR": "10.0.0.2"}},
        ),
        ("path", {"path": "/one?query=1"}, {"path": "/two"}),
        ("method", {"path": "/one"}, {"path": "/one", "method": "POST"}),
        (
            "user_id",
            {"path": "/one", "headers": {"Authorization": "Bearer first"}},
            {"path": "/one", "headers": {"Authorization": "Bearer second"}},
        ),
    ],
)
def test_supplied_limiter_uses_the_explicit_key_selector(
    key_type, first_request, second_request
):
    app = flask.Flask(__name__)
    limiter = RateLimiter(
        RateLimiterConfig(rules=[RateLimitRule(1, 60)]),
        storage=MemoryStorage(clock=lambda: 120),
    )

    @app.route("/<value>", methods=["GET", "POST"])
    @rate_limit(1, 60, key_type=key_type, limiter=limiter)
    def endpoint(value):
        return value

    client = app.test_client()
    assert client.open(**first_request).status_code == 200
    assert client.open(**second_request).status_code == 200
    assert client.open(**first_request).status_code == 429


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": 0},
        {"window": 0},
        {"algorithm": "unknown"},
        {"key_type": "unknown"},
    ],
)
def test_supplied_limiter_does_not_skip_route_policy_validation(arguments):
    limiter = RateLimiter(RateLimiterConfig())
    with pytest.raises(ValueError):
        rate_limit(**{"limit": 1, "window": 60, **arguments}, limiter=limiter)(
            lambda: "ok"
        )


def test_route_denial_preserves_the_fixed_429_response():
    app = flask.Flask(__name__)
    limiter = RateLimiter(
        RateLimiterConfig(rules=[RateLimitRule(1, 60)], default_response_code=503),
        storage=MemoryStorage(clock=lambda: 120),
    )

    @app.get("/limited")
    @rate_limit(1, 60, limiter=limiter)
    def endpoint():
        return "ok"

    client = app.test_client()
    assert client.get("/limited").status_code == 200
    denied = client.get("/limited")
    assert denied.status_code == 429
    assert denied.get_json() == {
        "error": "Too Many Requests",
        "message": "Rate limit exceeded. Try again in 60.0 seconds.",
    }
    assert "Retry-After" not in denied.headers
