import json
from pathlib import Path
import socket
import subprocess
import sys
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from search_autocomplete import Autocomplete, Store, Trie, Unavailable
from search_autocomplete.http import Coordinator, server
from search_autocomplete.service import relevant_shards


DATA = [("twitter", 35), ("twitch", 29), ("twilight", 25), ("twin peak", 21),
        ("twitch prime", 18), ("twitter search", 14), ("twillo", 10), ("twin peak sf", 8)]


def seed(store, rows=DATA):
    for query, frequency in rows:
        for i in range(frequency):
            store.record(query + str(i), query, 100)


@pytest.fixture
def store():
    value = Store()
    yield value
    value.close()


def test_chapter_ranking_and_prefixes(store):
    seed(store)
    store.build(0, 200, shards=3)
    service = Autocomplete(store)
    assert service.suggest("TW") == tuple(DATA[:5])
    assert service.suggest("twin ") == (("twin peak", 21), ("twin peak sf", 8))
    assert service.suggest("zzz") == ()
    assert service.suggest("") == ()
    assert service.suggest("witter") == ()
    assert relevant_shards(store.snapshot(), "tw") == [0, 1, 2]


def test_ties_trie_and_phrase_normalization(store):
    store.record("1", "  NEW   YORK ", 1)
    store.record("2", "newark", 1)
    store.build(0, 2)
    service = Autocomplete(store)
    assert service.suggest("new") == (("new york", 1), ("newark", 1))
    assert service.suggest("new ") == (("new york", 1),)
    assert Trie([("a", 3), ("ab", 2)]).suggest("a") == (("a", 3), ("ab", 2))


@pytest.mark.parametrize("query", [None, 3, "", " ", "é", "hello!", "a1", "a" * 51])
def test_invalid_query(store, query):
    with pytest.raises(ValueError):
        store.record("event", query, 0)


@pytest.mark.parametrize("at", [-1, float("nan"), float("inf"), True, "now"])
def test_invalid_timestamp(store, at):
    with pytest.raises(ValueError):
        store.record("event", "hello", at)


def test_deduplication_and_conflicts(store):
    assert store.record("same", "hello", 1)
    assert not store.record("same", "HELLO", 1)
    with pytest.raises(ValueError):
        store.record("same", "other", 1)
    with pytest.raises(ValueError):
        store.record("same", "hello", 2)
    assert store.record("automatic", "hello")
    assert not store.record("automatic", "hello")
    store.build(0, time.time() + 1)
    assert Autocomplete(store).suggest("he") == (("hello", 2),)


def test_concurrent_event_retry_counted_once(store):
    with ThreadPoolExecutor(max_workers=16) as pool:
        assert sum(pool.map(lambda _: store.record("same", "hello", 1), range(50))) == 1
    store.build(0, 2)
    assert Autocomplete(store).suggest("h") == (("hello", 1),)


def test_explicit_windows_and_snapshot_visibility(store):
    store.record("early", "old", 0)
    store.record("included", "new", 10)
    store.record("end", "end", 20)
    first = store.build(0, 10)
    service = Autocomplete(store)
    assert service.suggest("old") == (("old", 1),)
    store.record("late", "old", 5)
    assert service.suggest("old") == (("old", 1),)
    second = store.build(10, 20)
    assert second > first
    assert service.suggest("old") == ()
    assert service.suggest("new") == (("new", 1),)
    assert service.suggest("end") == ()
    assert service.suggest("old", version=first) == (("old", 1),)


def test_failed_build_keeps_active_snapshot(store, monkeypatch):
    seed(store)
    version = store.build(0, 200)
    def fail(_):
        raise RuntimeError("simulated builder failure")
    monkeypatch.setattr("search_autocomplete.store.Trie", fail)
    with pytest.raises(RuntimeError):
        store.build(0, 200, 2)
    assert store.view()[0] == version
    assert Autocomplete(store).suggest("tw") == tuple(DATA[:5])


def test_restart_and_revision_persistence(tmp_path):
    path = tmp_path / "data.sqlite3"
    with_store = Store(path)
    seed(with_store)
    version = with_store.build(0, 200, 2)
    revision = with_store.block("twitter")
    with_store.close()
    recovered = Store(path)
    try:
        assert recovered.view()[:2] == (version, revision)
        assert Autocomplete(recovered).suggest("tw")[0] == ("twitch", 29)
    finally:
        recovered.close()


def test_immediate_filter_cache_refill_and_rebuild(store):
    seed(store)
    first = store.build(0, 200)
    service = Autocomplete(store, cache_size=1)
    assert service.suggest("tw") == tuple(DATA[:5])
    revision = store.block("twitter")
    assert service.suggest("tw") == tuple(DATA[1:6])
    with pytest.raises(Unavailable):
        service.suggest("tw", first, revision - 1)
    service.suggest("twi")
    assert len(service.cache) == 1
    second = store.build(0, 200)
    assert "twitter" not in dict(store.snapshot(second)["shards"][0])
    store.block("twitter", False)
    assert service.suggest("tw", first) == tuple(DATA[:5])
    assert service.suggest("tw", second)[0] == DATA[1]  # A rebuild restores physically excluded terms.
    store.build(0, 200)
    assert service.suggest("tw") == tuple(DATA[:5])


def test_disabled_cache_and_empty_generation(store):
    store.build(0, 1, 8)
    service = Autocomplete(store, cache_size=0)
    assert service.suggest("a") == ()
    assert not service.cache
    assert len(store.snapshot()["shards"]) == 1


@pytest.mark.parametrize("args", [(1, 0, 1), (-1, 2, 1), (0, float("nan"), 1), (0, 1, 0), (0, 1, 257)])
def test_invalid_build(store, args):
    with pytest.raises(ValueError):
        store.build(*args)
    assert store.view()[0] == 0


@contextmanager
def running(http):
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_address[1]}"
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)


def get(url):
    with urlopen(url, timeout=5) as response:
        assert response.headers["Cache-Control"] == "no-store"
        return json.load(response)


def test_actual_http_merge_replica_fallback_and_filter(store):
    seed(store)
    store.build(0, 200, 2)
    with running(server(Autocomplete(store), shard=0)) as a, running(server(Autocomplete(store), shard=1)) as b:
        coordinator = Coordinator(store, {0: ["http://127.0.0.1:1", a], 1: [b]}, timeout=.1)
        try:
            with running(server(coordinator)) as endpoint:
                result = get(endpoint + "/suggest?prefix=tw")
                assert [(i["query"], i["frequency"]) for i in result["suggestions"]] == DATA[:5]
                store.block("twitter")
                assert get(endpoint + "/suggest?prefix=tw")["suggestions"][0]["query"] == "twitch"
                with pytest.raises(HTTPError) as error:
                    get(endpoint + "/suggest?prefix=bad%21")
                assert error.value.code == 400
        finally:
            coordinator.close()


def test_missing_shard_and_mixed_generation_rejected(store):
    seed(store)
    store.build(0, 200, 2)
    stale = Store()
    seed(stale)
    stale.build(0, 200, 2)
    stale.build(0, 200, 2)
    store.build(0, 200, 2)
    current = store.build(0, 200, 2)
    with running(server(Autocomplete(stale), shard=0)) as wrong, running(server(Autocomplete(store), shard=1)) as b:
        coordinator = Coordinator(store, {0: [wrong], 1: [b]}, timeout=.1)
        try:
            with pytest.raises(Unavailable):
                coordinator.suggest("tw")  # stale replica does not have required generation.
            coordinator.replicas[0] = ["http://127.0.0.1:1"]
            with pytest.raises(Unavailable):
                coordinator.suggest("tw")
            assert current == 3
        finally:
            coordinator.close()
            stale.close()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_independent_process_replica_failure(tmp_path):
    path = tmp_path / "process.sqlite3"
    store = Store(path)
    seed(store)
    store.build(0, 200)
    ports = [free_port(), free_port()]
    children = []
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    coordinator = Coordinator(store, {0: [f"http://127.0.0.1:{port}" for port in ports]}, timeout=.2)
    try:
        for port in ports:
            child = subprocess.Popen([sys.executable, "-m", "search_autocomplete.cli", "--db", str(path),
                                      "node", "--shard", "0", "--port", str(port)],
                                     env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
            children.append(child)
            deadline = time.monotonic() + 10
            while True:
                try:
                    get(f"http://127.0.0.1:{port}/suggest?prefix=tw")
                    break
                except OSError:
                    if child.poll() is not None or time.monotonic() > deadline:
                        raise AssertionError("child node failed to start")
                    time.sleep(.02)
        assert coordinator.suggest("tw")[2] == tuple(DATA[:5])
        children[0].kill()
        children[0].wait(timeout=5)
        assert coordinator.suggest("tw")[2] == tuple(DATA[:5])
        children[1].kill()
        children[1].wait(timeout=5)
        with pytest.raises(Unavailable):
            coordinator.suggest("tw")
    finally:
        coordinator.close()
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()
        store.close()
