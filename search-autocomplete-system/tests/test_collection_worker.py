"""Sampled completed events and interruptible offline scheduling contracts."""

import threading

import pytest

from search_autocomplete import Collector, SnapshotWorker, Store, Unavailable


def test_sampling_is_stable_not_biased_by_query():
    store = Store()
    try:
        first, second = Collector(store, 10), Collector(store, 10)
        selected = [str(i) for i in range(1000) if first.selected(str(i))]
        assert selected == [str(i) for i in range(1000) if second.selected(str(i))]
        assert 60 < len(selected) < 140
        for i in range(1000):
            assert first.record(str(i), "apple", 1) == (str(i) in selected)
            assert not second.record(str(i), "apple", 1)
        store.build(0, 2)
        assert store.snapshot()["shards"] == [[["apple", len(selected)]]]
    finally:
        store.close()


@pytest.mark.parametrize("factor", [True, 0, -1, 1.0, "2"])
def test_invalid_sampling_factor(factor):
    with pytest.raises(ValueError):
        Collector(None, factor)


@pytest.mark.parametrize(
    "query,at", [("bad!", 1), ("apple", float("nan")), ("apple", True)]
)
def test_sampled_out_input_still_validated(query, at):
    collector = Collector(None, 10**30)
    with pytest.raises(ValueError):
        collector.record("event", query, at)


@pytest.mark.parametrize("name", ["interval", "window", "retry_delay"])
@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), float("inf"), "1"])
def test_invalid_worker_duration(name, value):
    with pytest.raises(ValueError):
        SnapshotWorker(None, **{name: value})


def test_worker_trailing_window_and_restart_durable(tmp_path):
    path = str(tmp_path / "worker.sqlite3")
    store = Store(path)
    try:
        store.record("old", "apple", 5)
        store.record("new", "zebra", 15)
        worker = SnapshotWorker(store, window=10, clock=lambda: 20)
        version = worker.run_once()
        assert store.snapshot()["shards"] == [[["zebra", 1]]]
    finally:
        store.close()
    reopened = Store(path)
    try:
        assert reopened.view()[0] == version
    finally:
        reopened.close()


def test_worker_retries_publication_race_then_stops_without_new_work():
    class Flaky:
        calls = 0

        def build(self, start, end, shards):
            self.calls += 1
            if self.calls == 1:
                raise Unavailable("filter changed")
            return 7

    store, stop, errors, versions = Flaky(), threading.Event(), [], []

    def published(version):
        versions.append(version)
        stop.set()

    SnapshotWorker(store, retry_delay=0.001).run(
        stop, on_error=errors.append, on_publish=published
    )
    assert store.calls == 2 and len(errors) == 1 and versions == [7]
    SnapshotWorker(store).run(stop)
    assert store.calls == 2


def test_worker_can_purge_blocked_sources_before_rebuild():
    store = Store()
    try:
        store.record("a", "apple", 1)
        store.block("apple")
        SnapshotWorker(store, purge_blocked=True).run_once(2)
        store.block("apple", False)
        SnapshotWorker(store).run_once(2)
        assert store.snapshot()["shards"] == [[]]
        assert not store.record("a", "apple", 1)
    finally:
        store.close()
