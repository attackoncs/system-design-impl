"""Concurrent acceptance, offline build fences, and maintenance regressions."""

import concurrent.futures
import contextlib
import threading

import pytest
import search_autocomplete
import search_autocomplete.trie


def test_separate_connections_accept_one_identical_retry(tmp_path):
    path = tmp_path / "events.sqlite3"
    stores = [search_autocomplete.Store(path) for _ in range(8)]
    gate = threading.Barrier(len(stores))

    def accept(store):
        gate.wait(timeout=5)
        return store.record("shared", "hello", 1)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(stores)) as pool:
            assert sum(pool.map(accept, stores)) == 1
        with pytest.raises(ValueError, match="conflicting"):
            stores[-1].record("shared", "other", 1)
        stores[0].build(0, 2)
        assert search_autocomplete.Autocomplete(stores[0]).suggest("h") == (
            ("hello", 1),
        )
    finally:
        for store in stores:
            store.close()


@pytest.mark.parametrize("path_kind", ["memory", "file"])
def test_build_construction_does_not_block_ingestion_or_current_view(
    tmp_path, monkeypatch, path_kind
):
    store = search_autocomplete.Store(
        ":memory:" if path_kind == "memory" else tmp_path / "build.sqlite3"
    )
    store.record("first", "hello", 1)
    initial = store.build(0, 2)
    entered, release = threading.Event(), threading.Event()
    actual_trie = search_autocomplete.trie.Trie

    def slow_trie(rows):
        if threading.current_thread().name.startswith("offline"):
            entered.set()
            assert release.wait(timeout=5)
        return actual_trie(rows)

    monkeypatch.setattr(search_autocomplete.trie, "Trie", slow_trie)
    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="offline"
        ) as pool:
            future = pool.submit(store.build, 0, 2)
            try:
                assert entered.wait(timeout=5)
                assert store.record("during", "hello", 1)
                assert store.view()[0] == initial
                assert store.snapshot(initial)["event_watermark"] == 1
            finally:
                release.set()
            constructed = future.result(timeout=5)
        assert store.snapshot(constructed)["shards"] == [[["hello", 1]]]
        latest = store.build(0, 2)
        assert store.snapshot(latest)["shards"] == [[["hello", 2]]]
        assert store.snapshot(latest)["event_watermark"] == 2
    finally:
        release.set()
        store.close()


def test_older_build_cannot_overwrite_newer_cross_connection_publication(
    tmp_path, monkeypatch
):
    path = tmp_path / "publish.sqlite3"
    older, newer = search_autocomplete.Store(path), search_autocomplete.Store(path)
    older.record("first", "hello", 1)
    entered, release = threading.Event(), threading.Event()
    actual_trie = search_autocomplete.trie.Trie

    def slow_trie(rows):
        if threading.current_thread().name.startswith("older"):
            entered.set()
            assert release.wait(timeout=5)
        return actual_trie(rows)

    monkeypatch.setattr(search_autocomplete.trie, "Trie", slow_trie)
    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="older"
        ) as pool:
            future = pool.submit(older.build, 0, 2)
            try:
                assert entered.wait(timeout=5)
                newer.record("second", "hello", 1)
                version = newer.build(0, 2)
            finally:
                release.set()
            with pytest.raises(search_autocomplete.Unavailable, match="superseded"):
                future.result(timeout=5)
        assert older.view()[0] == version
        assert older.snapshot()["shards"] == [[["hello", 2]]]
    finally:
        release.set()
        older.close()
        newer.close()


def test_filter_change_invalidates_in_progress_build(tmp_path, monkeypatch):
    store = search_autocomplete.Store(tmp_path / "filter.sqlite3")
    store.record("first", "hello", 1)
    initial = store.build(0, 2)
    entered, release = threading.Event(), threading.Event()
    actual_trie = search_autocomplete.trie.Trie

    def slow_trie(rows):
        entered.set()
        assert release.wait(timeout=5)
        return actual_trie(rows)

    monkeypatch.setattr(search_autocomplete.trie, "Trie", slow_trie)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(store.build, 0, 2)
            try:
                assert entered.wait(timeout=5)
                revision = store.block("hello")
            finally:
                release.set()
            with pytest.raises(search_autocomplete.Unavailable, match="filter changed"):
                future.result(timeout=5)
        assert store.view()[:2] == (initial, revision)
    finally:
        release.set()
        store.close()


def test_aggregation_is_consistent_during_parallel_ingestion(tmp_path, monkeypatch):
    path = tmp_path / "aggregation.sqlite3"
    store, writer = search_autocomplete.Store(path), search_autocomplete.Store(path)
    store.record("before", "hello", 1)
    entered, release = threading.Event(), threading.Event()
    actual_reader = store._reader

    class SlowReader:
        def __init__(self, reader):
            self.reader = reader

        def execute(self, sql, params=()):
            if "COUNT(*)" in sql:
                entered.set()
                assert release.wait(timeout=5)
            return self.reader.execute(sql, params)

    @contextlib.contextmanager
    def reader():
        with actual_reader() as connection:
            yield SlowReader(connection)

    monkeypatch.setattr(store, "_reader", reader)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(store.aggregate, 0, 2)
            try:
                assert entered.wait(timeout=5)
                assert writer.record("during", "hello", 1)
            finally:
                release.set()
            aggregation_id = future.result(timeout=5)
        first = store.aggregation(aggregation_id)
        assert first["frequencies"] == [["hello", 1]]
        assert first["event_watermark"] == 1
        assert store.aggregation(store.aggregate(0, 2))["event_watermark"] == 2
    finally:
        release.set()
        store.close()
        writer.close()


def test_materialized_aggregation_survives_restart_and_builds_exact_window(tmp_path):
    path = tmp_path / "weekly.sqlite3"
    store = search_autocomplete.Store(path)
    store.record("week one", "hello", 1)
    store.record("week two", "other", 604800)
    aggregation_id = store.aggregate(0, 604800)
    expected = store.aggregation(aggregation_id)
    store.close()
    recovered = search_autocomplete.Store(path)
    try:
        assert recovered.aggregation(aggregation_id) == expected
        recovered.record("late", "hello", 2)
        version = recovered.build(0, 604800, aggregation_id=aggregation_id)
        assert recovered.snapshot(version)["shards"] == [[["hello", 1]]]
        with pytest.raises(ValueError, match="time window"):
            recovered.build(0, 604801, aggregation_id=aggregation_id)
        recovered.block("hello")
        with pytest.raises(search_autocomplete.Unavailable, match="filter changed"):
            recovered.build(0, 604800, aggregation_id=aggregation_id)
    finally:
        recovered.close()


def test_purge_preserves_idempotency_and_immutable_snapshot(tmp_path):
    path = tmp_path / "purge.sqlite3"
    store = search_autocomplete.Store(path)
    store.record("blocked event", "hello", 1)
    store.record("kept event", "other", 1)
    version = store.build(0, 2, 2)
    snapshot = store.snapshot(version)
    aggregation_id = snapshot["aggregation_id"]
    store.block("hello")
    assert store.purge_blocked() == 1
    assert store.purge_blocked() == 0
    assert store.snapshot(version) == snapshot
    with pytest.raises(search_autocomplete.Unavailable):
        store.aggregation(aggregation_id)
    assert store.db.execute("SELECT query FROM events").fetchall() == [("other",)]
    store.close()
    recovered = search_autocomplete.Store(path)
    try:
        assert not recovered.record("blocked event", "hello", 1)
        with pytest.raises(ValueError, match="conflicting"):
            recovered.record("blocked event", "other", 1)
        with pytest.raises(ValueError, match="conflicting"):
            recovered.record("blocked event", "hello", 2)
        recovered.block("hello", False)
        rebuilt = recovered.build(0, 2)
        assert recovered.snapshot(rebuilt)["shards"] == [[["other", 1]]]
        assert recovered.record("fresh event", "hello", 1)
        assert recovered.aggregation(recovered.aggregate(0, 2))["event_watermark"] == 3
    finally:
        recovered.close()


def test_close_waits_for_active_build_and_rejects_new_work(tmp_path, monkeypatch):
    path = tmp_path / "closing.sqlite3"
    store = search_autocomplete.Store(path)
    store.record("first", "hello", 1)
    entered, release, closing = threading.Event(), threading.Event(), threading.Event()
    actual_trie = search_autocomplete.trie.Trie

    def slow_trie(rows):
        entered.set()
        assert release.wait(timeout=5)
        return actual_trie(rows)

    def close():
        closing.set()
        store.close()

    monkeypatch.setattr(search_autocomplete.trie, "Trie", slow_trie)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            build = pool.submit(store.build, 0, 2)
            try:
                assert entered.wait(timeout=5)
                closed = pool.submit(close)
                assert closing.wait(timeout=5)
                with store._condition:
                    assert store._condition.wait_for(lambda: store._closing, timeout=5)
                assert not closed.done()
                with pytest.raises(RuntimeError, match="closing"):
                    store.record("new", "hello", 1)
            finally:
                release.set()
            version = build.result(timeout=5)
            closed.result(timeout=5)
        recovered = search_autocomplete.Store(path)
        try:
            assert recovered.view()[0] == version
        finally:
            recovered.close()
    finally:
        release.set()
        store.close()


@pytest.mark.parametrize("enabled", [0, 1, None, "false", [], {}])
def test_block_requires_actual_boolean(enabled):
    store = search_autocomplete.Store()
    try:
        with pytest.raises(ValueError, match="boolean"):
            store.block("hello", enabled)
        assert store.view()[1] == 0
    finally:
        store.close()


def test_existing_database_migrates_without_overwriting_generation(tmp_path):
    import json
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    snapshot = {
        "version": 7,
        "start": 0,
        "end": 2,
        "shards": [[["hello", 1]]],
        "lower": ["hello"],
    }
    with sqlite3.connect(path) as database:
        database.executescript(
            "CREATE TABLE events(id TEXT PRIMARY KEY, query TEXT NOT NULL, at REAL NOT NULL);"
            "CREATE TABLE snapshots(id INTEGER PRIMARY KEY, data TEXT NOT NULL);"
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL);"
            "CREATE TABLE blocked(query TEXT PRIMARY KEY);"
            "INSERT INTO events VALUES('existing', 'hello', 1);"
            "INSERT INTO meta VALUES('active', 7), ('revision', 0);"
        )
        database.execute(
            "INSERT INTO snapshots VALUES(?, ?)", (7, json.dumps(snapshot))
        )
    store = search_autocomplete.Store(path)
    try:
        assert store.snapshot() == snapshot
        version = store.build(0, 2)
        assert version > 7
        assert store.snapshot(7) == snapshot
        assert store.snapshot(version)["event_watermark"] == 1
        assert not store.record("existing", "hello", 1)
    finally:
        store.close()


def test_concurrent_initialization_and_purged_retry_are_safe(tmp_path):
    path = tmp_path / "initialization.sqlite3"
    gate = threading.Barrier(6)

    def open_store(_):
        gate.wait(timeout=5)
        return search_autocomplete.Store(path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        stores = list(pool.map(open_store, range(6)))
    try:
        stores[0].record("event", "hello", 1)
        stores[0].block("hello")
        stores[0].purge_blocked()
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            assert not any(
                pool.map(lambda store: store.record("event", "hello", 1), stores)
            )
        assert stores[-1].db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    finally:
        for store in stores:
            store.close()


@pytest.mark.parametrize("value", [10**1000, -(10**1000)])
def test_unrepresentable_numeric_inputs_raise_value_error(value):
    store = search_autocomplete.Store()
    try:
        with pytest.raises(ValueError):
            store.record("event", "hello", value)
        with pytest.raises(ValueError):
            store.build(0, value)
    finally:
        store.close()


def test_filter_view_cache_tracks_persisted_revision_across_instances(tmp_path):
    path = tmp_path / "filter-cache.sqlite3"
    writer, reader = search_autocomplete.Store(path), search_autocomplete.Store(path)
    statements = []
    reader.db.set_trace_callback(statements.append)
    try:
        assert writer.block("hello") == 1
        first = reader.view()[2]
        assert first == frozenset({"hello"})
        for _ in range(5):
            assert reader.view()[2] is first
            assert reader.revision() == 1
        assert sum(sql == "SELECT query FROM blocked" for sql in statements) == 1
        assert writer.block("hello") == 1
        assert reader.view()[2] is first
        assert writer.block("other") == 2
        assert reader.revision() == 2
        assert sum(sql == "SELECT query FROM blocked" for sql in statements) == 1
        second = reader.view()[2]
        assert second == frozenset({"hello", "other"})
        assert first == frozenset({"hello"})
        assert sum(sql == "SELECT query FROM blocked" for sql in statements) == 2
        assert writer.block("hello", False) == 3
        assert reader.view()[1:] == (3, frozenset({"other"}))
    finally:
        reader.db.set_trace_callback(None)
        writer.close()
        reader.close()
