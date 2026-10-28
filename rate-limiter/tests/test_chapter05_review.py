"""Chapter-level policy, clock, capacity and publication regressions."""

import json
import os
import runpy
import queue
import time
from pathlib import Path
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from rate_limiter.algorithms.base import RateLimitResult
from rate_limiter.config import Algorithm, RateLimiterConfig, RateLimitRule
from rate_limiter.core import RateLimiter
from rate_limiter.keys import ip_key, path_key, user_id_key
from rate_limiter.rule_files import RuleReloader
from rate_limiter.queue import LeakyBucketQueue
from rate_limiter.storage.memory import MemoryStorage


def request(path="/a", user="u", host="127.0.0.1"):
    return SimpleNamespace(
        path=path,
        user=SimpleNamespace(id=user),
        client=SimpleNamespace(host=host),
        headers={},
    )


def test_paths_key_types_names_and_duplicate_rules_are_effective():
    a = RateLimitRule(1, 60, key_type="user_id", path_pattern="/a/*", name="a")
    b = RateLimitRule(1, 60, key_type="user_id", path_pattern="/b/*", name="b")
    limiter = RateLimiter(RateLimiterConfig(rules=[a, a, b]))
    assert limiter.check_request(request("/a/1")).allowed
    assert not limiter.check_request(request("/a/2")).allowed
    assert limiter.check_request(request("/a/2", user="v")).allowed
    assert limiter.check_request(request("/b/1")).allowed
    assert limiter.check_request(request("/unlimited")).limit == 0
    assert limiter.metrics() == {
        "checks": 4,
        "allowed": 3,
        "denied": 1,
        "storage_errors": 0,
    }


def test_configuration_does_not_alias_callers_or_readers():
    original = RateLimitRule(1, 60)
    config = RateLimiterConfig(rules=[original])
    limiter = RateLimiter(config)
    original.limit = 99
    config.rules.clear()
    limiter.config.rules[0].limit = 50
    assert limiter.check_request("subject").allowed
    assert not limiter.check_request("subject").allowed
    limiter.update_rules([original])
    original.limit = 0
    assert limiter.config.rules[0].limit == 99


@pytest.mark.parametrize("algorithm", list(Algorithm))
def test_clock_regression_never_resets_quota(algorithm):
    now = [1001.0]
    limiter = RateLimiter(
        RateLimiterConfig(fail_open=False), storage=MemoryStorage(clock=lambda: now[0])
    )
    rule = RateLimitRule(1, 60, algorithm)
    assert limiter.check("clock", rule).allowed
    now[0] = 900.0
    denied = limiter.check("clock", rule)
    assert not denied.allowed
    assert denied.retry_after >= 101
    now[0] = 1002.0
    assert not limiter.check("clock", rule).allowed


def test_sampling_occurs_after_the_storage_lock_is_acquired():
    sampled, entered = threading.Event(), threading.Event()
    storage = MemoryStorage(clock=lambda: (sampled.set(), 1000)[1])
    limiter = RateLimiter(RateLimiterConfig(), storage=storage)
    rule = RateLimitRule(1, 60)
    with ThreadPoolExecutor(1) as pool:
        with storage._lock:

            def check():
                entered.set()
                return limiter.check("lock", rule)

            future = pool.submit(check)
            assert entered.wait(1)
            assert not sampled.is_set()
        assert future.result(timeout=2).allowed
    assert sampled.is_set()


@pytest.mark.parametrize("algorithm", list(Algorithm))
def test_shared_memory_has_exact_concurrent_admission(algorithm):
    limiter = RateLimiter(
        RateLimiterConfig(fail_open=False), storage=MemoryStorage(clock=lambda: 1001)
    )
    rule = RateLimitRule(17, 3600, algorithm)
    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(lambda _: limiter.check("same", rule), range(200)))
    assert sum(result.allowed for result in results) == 17
    assert limiter.metrics()["storage_errors"] == 0


def test_leaking_bucket_fractional_occupancy_and_exact_retry():
    now = [1000.0]
    limiter = RateLimiter(
        RateLimiterConfig(), storage=MemoryStorage(clock=lambda: now[0])
    )
    rule = RateLimitRule(2, 10, Algorithm.LEAKING_BUCKET)
    assert limiter.check("leak", rule).allowed
    assert limiter.check("leak", rule).allowed
    now[0] += 1
    denied = limiter.check("leak", rule)
    assert not denied.allowed  # Occupancy 1.8 cannot accept another whole request.
    assert denied.retry_after == pytest.approx(4)
    now[0] += 4
    assert limiter.check("leak", rule).allowed


@pytest.mark.parametrize(
    "values",
    [
        [2, 0, 1, 1, 0],
        [1, -1, 1, 1, 0],
        [0, 1, 1, 1, 1],
        [1, 0, 1, float("nan"), 0],
        [0, 0, 1, 1, float("inf")],
        [1, 0.5, 1, 1, 0],
        [0, 0, 1, 1, 0],
        [1, 0, 1, 1],
    ],
)
def test_malformed_storage_results_are_rejected(values):
    with pytest.raises(ValueError):
        RateLimitResult.from_storage(values)


def test_capacity_does_not_evict_live_quotas_and_sweeps_idle_expiry(monkeypatch):
    tick = [1.0]
    monkeypatch.setattr("rate_limiter.storage.memory.time.monotonic", lambda: tick[0])
    storage = MemoryStorage(max_keys=4)
    for n in range(4):
        storage.set(str(n), "1", ttl=1)
    with pytest.raises(MemoryError):
        storage.set("overflow", "1", ttl=10)
    assert storage.get("0") == "1"
    tick[0] = 3
    storage.set("next", "2", ttl=10)
    assert list(storage._data) == ["next"]


def test_file_reload_is_complete_strict_and_preserves_previous_on_error(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"rules": [{"limit": 1, "window": 60}]}))
    limiter = RateLimiter(RateLimiterConfig())
    worker = RuleReloader(limiter, path, interval=0.05)
    assert worker.poll()
    assert not worker.poll()
    assert limiter.check_request("file").allowed
    path.write_text('{"rules":[],"rules":[]}')
    assert not worker.poll()
    assert not limiter.check_request("file").allowed
    path.write_text(json.dumps({"rules": [{"limit": 2, "window": 60}]}))
    with worker:
        assert worker.status()["reloads"] == 2
        assert limiter.config.rules[0].limit == 2
    assert not worker._thread.is_alive()


def test_ip_headers_are_untrusted_by_default_and_paths_ignore_queries():
    req = request()
    req.headers["X-Forwarded-For"] = "10.1.2.3"
    assert ip_key(req) == "127.0.0.1"
    assert ip_key(req, trust_forwarded=True) == "10.1.2.3"
    assert path_key(SimpleNamespace(url="https://example.com/a?b=1")) == "/a"


def test_credential_keys_are_stable_in_separate_python_processes():
    script = "from types import SimpleNamespace; from rate_limiter.keys import user_id_key; print(user_id_key(SimpleNamespace(headers={'Authorization':'Bearer token'})))"
    expected = user_id_key(SimpleNamespace(headers={"Authorization": "Bearer token"}))
    outputs = [
        subprocess.check_output(
            [sys.executable, "-c", script],
            text=True,
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            },
        ).strip()
        for _ in range(2)
    ]
    assert outputs == [expected, expected]


@pytest.mark.parametrize(
    "algorithm", [Algorithm.TOKEN_BUCKET, Algorithm.LEAKING_BUCKET]
)
def test_burst_capacity_is_independent_of_steady_rate(algorithm):
    now = [1000.0]
    limiter = RateLimiter(
        RateLimiterConfig(fail_open=False), storage=MemoryStorage(clock=lambda: now[0])
    )
    rule = RateLimitRule(2, 10, algorithm, capacity=5)
    assert all(limiter.check("burst", rule).allowed for _ in range(5))
    assert not limiter.check("burst", rule).allowed
    now[0] += 5
    result = limiter.check("burst", rule)
    assert result.allowed and result.limit == 5
    assert not limiter.check("burst", rule).allowed


def test_fifo_execution_is_bounded_paced_and_closes():
    calls = []
    worker = LeakyBucketQueue(capacity=3, rate=50)
    futures = [
        worker.submit(lambda n=n: calls.append((n, time.monotonic()))) for n in range(3)
    ]
    with pytest.raises(queue.Full):
        worker.submit(lambda: None)
    with worker:
        for future in futures:
            future.result(timeout=2)
    assert [item[0] for item in calls] == [0, 1, 2]
    assert all(b[1] - a[1] >= 0.018 for a, b in zip(calls, calls[1:]))
    assert not worker._thread.is_alive()
    with pytest.raises(RuntimeError):
        worker.submit(lambda: None)


def test_fifo_close_cancels_waiting_work_and_forwards_errors():
    worker = LeakyBucketQueue(capacity=2, rate=1)
    future = worker.submit(lambda: None)
    worker.close()
    assert future.cancelled()
    with LeakyBucketQueue(capacity=1, rate=100) as worker:
        future = worker.submit(lambda: 1 / 0)
        with pytest.raises(ZeroDivisionError):
            future.result(timeout=1)


@pytest.mark.parametrize("framework", ["flask", "fastapi"])
@pytest.mark.parametrize(
    "auto_headers,include_headers", [(True, True), (False, True), (True, False)]
)
def test_http_adapters_share_status_and_header_policy(
    framework, auto_headers, include_headers
):
    config = RateLimiterConfig(
        rules=[RateLimitRule(1, 60)],
        default_response_code=503,
        include_headers=include_headers,
    )
    limiter = RateLimiter(config)
    if framework == "flask":
        from flask import Flask
        from rate_limiter.middleware.flask import RateLimitExtension

        app = Flask(__name__)
        app.add_url_rule("/", view_func=lambda: "ok")
        RateLimitExtension(app, limiter=limiter, auto_headers=auto_headers)
        client = app.test_client()
    else:
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        from rate_limiter.middleware.fastapi import RateLimitMiddleware

        app = FastAPI()
        app.add_api_route("/", lambda: "ok")
        app.add_middleware(
            RateLimitMiddleware, limiter=limiter, auto_headers=auto_headers
        )
        client = TestClient(app)
    try:
        assert client.get("/").status_code == 200
        denied = client.get("/")
        assert denied.status_code == 503
        if auto_headers and include_headers:
            assert denied.headers["Retry-After"].isdigit()
            assert (
                denied.headers["X-RateLimit-Retry-After"]
                == denied.headers["Retry-After"]
            )
        else:
            assert "X-RateLimit-Limit" not in denied.headers
            assert "Retry-After" not in denied.headers
    finally:
        if framework == "fastapi":
            client.close()


def test_bad_reload_cannot_publish_half_a_rule_set(tmp_path):
    limiter = RateLimiter(RateLimiterConfig(rules=[RateLimitRule(1, 60)]))
    path = tmp_path / "rules.json"
    path.write_text(
        json.dumps(
            {"rules": [{"limit": 50, "window": 60}, {"limit": False, "window": 60}]}
        )
    )
    with pytest.raises(ValueError):
        limiter.reload_rules(path)
    assert limiter.config.rules == [RateLimitRule(1, 60)]


def test_direct_checks_isolate_distinct_rule_parameters():
    limiter = RateLimiter(RateLimiterConfig())
    assert limiter.check("user", RateLimitRule(1, 60)).allowed
    assert limiter.check("user", RateLimitRule(2, 60)).remaining == 1
    assert not limiter.check("user", RateLimitRule(1, 60)).allowed


@pytest.mark.parametrize("clock", [float("nan"), float("inf"), -1])
def test_bad_clock_is_a_visible_storage_failure_before_mutation(clock):
    storage = MemoryStorage(clock=lambda: clock)
    limiter = RateLimiter(RateLimiterConfig(fail_open=False), storage=storage)
    assert not limiter.check("clock", RateLimitRule(1, 60)).allowed
    assert limiter.metrics()["storage_errors"] == 1
    assert not storage._data


def test_full_storage_cleanup_is_bounded_and_never_evicts_live_counters():
    storage = MemoryStorage(max_keys=1000)
    for index in range(1000):
        storage.set(str(index), "1", ttl=60)
    visited = []
    original = storage._sweep

    def sweep(budget=64, **kwargs):
        visited.append(budget)
        return original(budget, **kwargs)

    storage._sweep = sweep
    with pytest.raises(MemoryError):
        storage.set("full", "1", ttl=60)
    assert max(visited) == 128
    assert len(storage._data) == 1000


@pytest.mark.parametrize("framework", ["flask", "fastapi"])
def test_documented_framework_examples_import_and_serve(framework):
    path = Path(__file__).resolve().parents[1] / "examples" / f"{framework}_example.py"
    app = runpy.run_path(str(path))["app"]
    if framework == "flask":
        assert app.test_client().get("/").status_code == 200
    else:
        from starlette.testclient import TestClient

        with TestClient(app) as client:
            assert client.get("/").status_code == 200
