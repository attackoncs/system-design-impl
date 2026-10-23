"""Behavioral regressions for routing, cache ownership and worker cleanup."""

from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import Mock

import pytest

from scale_from_zero.balancer import LoadBalancer
from scale_from_zero.cache import Cache
from scale_from_zero.common import Error
from scale_from_zero.workers import PeriodicWorker, consume_remote_once


@pytest.mark.parametrize(
    "path,body,retries",
    [
        ("/v1/login", {"request_id": "looks-idempotent"}, 1),
        ("/v1/profile", {"request_id": "write-1"}, 2),
        ("/v1/profile", {}, 1),
    ],
)
def test_write_replay_depends_on_endpoint_contract(path, body, retries):
    transport = Mock(side_effect=OSError("response lost"))
    balancer = LoadBalancer(["http://one", "http://two"], transport)
    with pytest.raises(Error) as error:
        balancer.forward("POST", path, "token", body)
    assert error.value.status == 503
    assert transport.call_count == retries


def test_balancer_never_holds_routing_lock_during_io():
    entered, release = threading.Event(), threading.Event()

    def transport(url, *args, **kwargs):
        if url.startswith("http://one"):
            entered.set()
            assert release.wait(3)
        return url

    balancer = LoadBalancer(["http://one", "http://two"], transport)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(balancer.forward, "GET", "/v1/profile", "token")
        try:
            assert entered.wait(1)
            second = pool.submit(balancer.forward, "GET", "/v1/profile", "token")
            assert second.result(1) == "http://two/v1/profile"
        finally:
            release.set()
        assert first.result(2) == "http://one/v1/profile"


def test_balancer_does_not_retry_client_errors():
    transport = Mock(side_effect=Error(409, "conflict"))
    with pytest.raises(Error, match="conflict"):
        LoadBalancer(["a", "b"], transport).forward("GET", "/v1/profile", "")
    assert transport.call_count == 1


def test_cache_concurrent_eviction_and_exact_hit_accounting():
    cache = Cache(capacity=4)

    def access(index):
        for offset in range(100):
            key = str((index + offset) % 8)
            cache.put(key, {"version": 1, "profile": {"name": "original"}})
            value = cache.get(key)
            if value:
                value["profile"]["name"] = "changed"
            if offset % 3 == 0:
                cache.delete(key)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(access, range(8)))
    status = cache.status()
    assert status["entries"] <= 4
    assert status["hits"] + status["misses"] == 800
    for key in list(cache.values):
        assert cache.get(key)["profile"]["name"] == "original"


def test_periodic_worker_closes_after_inflight_step():
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def step():
        entered.set()
        assert release.wait(3)

    worker = PeriodicWorker(step, "test-worker", interval=100)
    worker.start()
    assert entered.wait(1)

    def close():
        worker.close()
        closed.set()

    thread = threading.Thread(target=close)
    thread.start()
    try:
        assert not closed.wait(0.05)
    finally:
        release.set()
        thread.join(2)
    assert closed.is_set()
    assert not worker._thread.is_alive()


def test_unavailable_shard_does_not_starve_other_outbox():
    job = {"id": "job-1"}
    transport = Mock(
        side_effect=[OSError("offline"), {"job": job}, {"acknowledged": True}]
    )
    consumer = Mock()
    consumer.consume.return_value = True
    consume_remote_once(["http://one", "http://two"], "token", consumer, transport)
    consumer.consume.assert_called_once_with(job)
    assert transport.call_args.args[0] == "http://two/internal/ack"
