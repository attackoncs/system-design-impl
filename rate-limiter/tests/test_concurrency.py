"""Concurrency and backend-dispatch regressions using public APIs."""

from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import patch

import pytest
from rate_limiter.config import Algorithm, RateLimitRule, RateLimiterConfig
from rate_limiter.core import RateLimiter
from rate_limiter.storage.memory import MemoryStorage


@pytest.mark.parametrize("algorithm", list(Algorithm))
def test_concurrent_requests_obey_limit(algorithm):
    rule = RateLimitRule(limit=7, window=60, algorithm=algorithm)
    limiter = RateLimiter(RateLimiterConfig(rules=[rule]))
    with patch("time.time", return_value=120.0):
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: limiter.check("user", rule), range(100)))
    assert sum(result.allowed for result in results) == 7


def test_public_operations_inside_atomic_callback_do_not_deadlock():
    store = MemoryStorage()
    done = threading.Event()
    result = []

    def run():
        result.append(
            store.execute_atomic(
                lambda keys, args, storage: storage.increment(keys[0]), ["n"], []
            )
        )
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert done.wait(2), "atomic callback deadlocked on public increment"
    thread.join(2)
    assert result == [1]


def test_ttl_uses_monotonic_deadline_and_expires_at_boundary():
    store = MemoryStorage()
    with patch("time.monotonic", return_value=10):
        store.set("key", "value", ttl=5)
    with (
        patch("time.monotonic", return_value=14),
        patch("time.time", return_value=-9999),
    ):
        assert store.get("key") == "value"
    with patch("time.monotonic", return_value=15):
        assert store.get("key") is None


def test_shared_storage_is_injectable():
    store = MemoryStorage()
    rule = RateLimitRule(limit=1, window=60, algorithm=Algorithm.FIXED_WINDOW)
    config = RateLimiterConfig(rules=[rule])
    first, second = (
        RateLimiter(config, storage=store),
        RateLimiter(config, storage=store),
    )
    with patch("time.time", return_value=120):
        assert first.check("shared", rule).allowed
        assert not second.check("shared", rule).allowed


def test_multiple_rules_return_longest_retry_delay():
    from rate_limiter.algorithms.base import RateLimitResult

    rules = [RateLimitRule(limit=1, window=60), RateLimitRule(limit=1, window=600)]
    limiter = RateLimiter(RateLimiterConfig(rules=rules))
    fast = RateLimitResult(False, 0, 1, 10, 10)
    slow = RateLimitResult(False, 0, 1, 100, 100)
    with patch.object(limiter, "check", side_effect=[fast, slow]):
        assert limiter.check_request("user").retry_after == 100
