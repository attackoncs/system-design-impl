"""Numeric entry points reject integers outside finite floating-point range."""

import pytest

from search_autocomplete import Collector, SnapshotWorker


@pytest.mark.parametrize(
    "value", [10**1000, -(10**1000)], ids=["huge-positive", "huge-negative"]
)
def test_collector_rejects_unrepresentable_event_time(value):
    collector = Collector(None, sample_every=10**30)
    with pytest.raises(ValueError, match="invalid event time"):
        collector.record("event", "hello", value)


@pytest.mark.parametrize("name", ["interval", "window", "retry_delay"])
@pytest.mark.parametrize(
    "value", [10**1000, -(10**1000)], ids=["huge-positive", "huge-negative"]
)
def test_worker_rejects_unrepresentable_duration(name, value):
    with pytest.raises(ValueError, match=f"{name} must be positive and finite"):
        SnapshotWorker(None, **{name: value})


@pytest.mark.parametrize("from_clock", [False, True])
@pytest.mark.parametrize(
    "value", [10**1000, -(10**1000)], ids=["huge-positive", "huge-negative"]
)
def test_worker_rejects_unrepresentable_window_end(value, from_clock):
    worker = SnapshotWorker(None, clock=lambda: value)
    with pytest.raises(ValueError, match="window end must be positive and finite"):
        worker.run_once() if from_clock else worker.run_once(value)
