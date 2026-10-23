"""Regressions for same-tick requests, async decorators and invalid rules."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import inspect
import threading
from unittest.mock import Mock

import fakeredis
import pytest

from rate_limiter.algorithms.base import RateLimitResult
from rate_limiter.algorithms.sliding_window_log import SlidingWindowLogAlgorithm
from rate_limiter.config import Algorithm, RateLimitRule
from rate_limiter.decorators import rate_limit
from rate_limiter.storage.memory import MemoryStorage
from rate_limiter.storage.redis import RedisStorage


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_same_tick_requests_count_individually(backend, monkeypatch):
    monkeypatch.setattr(
        "rate_limiter.algorithms.sliding_window_log.time.time", lambda: 1000.0
    )
    if backend == "memory":
        storage = MemoryStorage()
    else:
        monkeypatch.setattr(
            "redis.Redis.from_url", lambda *a, **k: fakeredis.FakeRedis()
        )
        storage = RedisStorage()
    rule = RateLimitRule(7, 60, Algorithm.SLIDING_WINDOW_LOG)
    algorithm = SlidingWindowLogAlgorithm()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: algorithm.check("same-tick", rule, storage), range(100))
        )
    assert sum(result.allowed for result in results) == 7
    assert sorted(result.remaining for result in results if result.allowed) == list(
        range(7)
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("limit", 0),
        ("limit", -1),
        ("limit", True),
        ("limit", 1.5),
        ("window", 0),
        ("window", False),
        ("window", "60"),
        ("algorithm", "token_bucket"),
        ("key_func", 42),
    ],
)
def test_rules_reject_invalid_configuration_before_requests(field, value):
    arguments = {"limit": 5, "window": 60, field: value}
    with pytest.raises(ValueError):
        RateLimitRule(**arguments)


def test_async_decorator_preserves_coroutine_and_event_loop_progress():
    entered, release = threading.Event(), threading.Event()
    limiter = Mock()

    def check(*args):
        entered.set()
        assert release.wait(2), "storage blocked event loop"
        return RateLimitResult(True, 0, 1, 1, None)

    limiter.check.side_effect = check

    @rate_limit(1, 60, limiter=limiter)
    async def endpoint():
        return "ok"

    assert inspect.iscoroutinefunction(endpoint)

    async def scenario():
        task = asyncio.create_task(endpoint())
        try:

            async def wait_for_storage():
                while not entered.is_set():
                    await asyncio.sleep(0.001)

            await asyncio.wait_for(wait_for_storage(), timeout=1)
            release.set()
            assert await task == "ok"
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_denied_async_decorator_does_not_invoke_handler():
    limiter = Mock()
    limiter.check.return_value = RateLimitResult(False, 0, 1, 60, 60)
    calls = []

    @rate_limit(1, 60, limiter=limiter, raise_exception=False)
    async def endpoint():
        calls.append(True)

    assert asyncio.run(endpoint())["status_code"] == 429
    assert calls == []


def test_storage_response_decodes_zero_bytes_as_denial():
    result = RateLimitResult.from_storage([b"0", b"0", b"5", b"3.5", b"1.5"])
    assert not result.allowed
    assert result.retry_after == 1.5
