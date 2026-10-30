"""Request observations must remain correct across failure, cancellation and load."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import AsyncMock

import pytest

from news_feed_system import FeedRequest, NewsFeedAPI, RequestMetrics
from news_feed_system.exceptions import AuthenticationError


def test_latency_outcomes_and_snapshots_with_an_injected_clock():
    now = [1000.0]
    metrics = RequestMetrics(clock=lambda: now[0])
    with metrics.measure("retrieve"):
        now[0] += 2
    with pytest.raises(ValueError, match="source unavailable"):
        with metrics.measure("retrieve"):
            now[0] += 3
            raise ValueError("source unavailable")
    with pytest.raises(asyncio.CancelledError):
        with metrics.measure("retrieve"):
            now[0] += 1
            raise asyncio.CancelledError()
    result = metrics.snapshot()["retrieve"]
    assert result["requests"] == 3
    assert result["in_flight"] == 0
    assert result["succeeded"] == result["failed"] == result["cancelled"] == 1
    assert result["latency_seconds"] == {
        "count": 3,
        "total": 6.0,
        "mean": 2.0,
        "max": 3.0,
    }
    result["latency_seconds"]["max"] = 999
    assert metrics.snapshot()["retrieve"]["latency_seconds"]["max"] == 3
    assert metrics.snapshot()["publish"]["latency_seconds"]["mean"] == 0


def test_qps_buckets_expire_and_reuse_without_resetting_lifetime_counts():
    now = [1000.0]
    metrics = RequestMetrics(clock=lambda: now[0])
    for _ in range(120):
        with metrics.measure("publish"):
            pass
    assert metrics.snapshot()["publish"]["qps_last_60s"] == 2
    now[0] += 59
    assert metrics.snapshot()["publish"]["requests_last_60s"] == 120
    now[0] += 1
    assert metrics.snapshot()["publish"]["qps_last_60s"] == 0
    with metrics.measure("publish"):
        pass
    result = metrics.snapshot()["publish"]
    assert result["requests"] == 121
    assert result["requests_last_60s"] == 1
    assert result["qps_last_60s"] == 1 / 60


def test_unknown_labels_cannot_grow_metric_cardinality():
    metrics = RequestMetrics()
    with pytest.raises(ValueError, match="Only publish and retrieve"):
        with metrics.measure("arbitrary-recipient-id"):
            pytest.fail("An unbounded label was accepted")
    assert set(metrics.snapshot()) == {"publish", "retrieve"}


def test_concurrent_observation_does_not_hold_lock_during_request_work():
    metrics = RequestMetrics()
    workers = 16
    entered = threading.Barrier(workers + 1)
    release = threading.Event()

    def observe():
        with metrics.measure("retrieve"):
            entered.wait(timeout=5)
            assert release.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = [pool.submit(observe) for _ in range(workers)]
        try:
            entered.wait(timeout=5)
            assert metrics.snapshot()["retrieve"]["in_flight"] == workers
        finally:
            release.set()
        for task in pending:
            task.result(timeout=5)
    result = metrics.snapshot()["retrieve"]
    assert result["requests"] == result["succeeded"] == workers
    assert result["in_flight"] == 0


async def test_api_measures_acceptance_and_failed_reads_without_payload_labels():
    api = NewsFeedAPI()
    api.auth.register_token("secret", "author")
    api.fanout.fanout = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    try:
        post = await api.publish(FeedRequest("author", "secret", "private body"))
        assert api.post_store.get(post) is not None
        with pytest.raises(AuthenticationError):
            await api.retrieve("invalid-token")
        with pytest.raises(AuthenticationError):
            await api.publish(FeedRequest("author", "invalid-token", "body"))
        result = api.metrics()
        assert result["publish"]["succeeded"] == 1
        assert result["publish"]["failed"] == 1
        assert result["retrieve"]["failed"] == 1
        assert result["publish"]["latency_seconds"]["count"] == 2
        assert "secret" not in str(result) and "private body" not in str(result)
    finally:
        await api.stop()


async def test_api_cancellation_releases_observed_in_flight_requests():
    api = NewsFeedAPI()
    entered = asyncio.Event()

    async def delayed_read(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    api.reader.retrieve = delayed_read
    reading = asyncio.create_task(api.retrieve("token"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert api.metrics()["retrieve"]["in_flight"] == 1
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
        result = api.metrics()["retrieve"]
        assert result["requests"] == result["cancelled"] == 1
        assert result["in_flight"] == 0
    finally:
        reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)
        await api.stop()
