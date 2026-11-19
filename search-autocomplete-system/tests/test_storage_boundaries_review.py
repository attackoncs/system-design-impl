"""Storage identity and SQLite integer boundary regressions."""

import threading
import urllib.error
import urllib.request

import pytest
from search_autocomplete import Autocomplete, Store, Unavailable
from search_autocomplete.http import server


def test_relative_file_store_keeps_its_database_after_working_directory_changes(
    tmp_path, monkeypatch
):
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    monkeypatch.chdir(first_dir)
    first = Store("events.sqlite3")
    try:
        first.record("first", "apple", 1)
        monkeypatch.chdir(second_dir)
        second = Store("events.sqlite3")
        try:
            second.record("second", "banana", 1)
        finally:
            second.close()

        aggregation = first.aggregate(0, 2)
        assert first.aggregation(aggregation)["frequencies"] == [["apple", 1]]
        version = first.build(0, 2, aggregation_id=aggregation)
        assert first.snapshot(version)["shards"] == [[["apple", 1]]]
    finally:
        first.close()


@pytest.mark.parametrize("method", ["snapshot", "aggregation"])
def test_persisted_identifiers_reject_values_above_sqlite_int64(method):
    store = Store()
    try:
        with pytest.raises(ValueError):
            getattr(store, method)(2**63)
        with pytest.raises(Unavailable):
            getattr(store, method)(2**63 - 1)
    finally:
        store.close()


def test_http_rejects_oversized_version_without_dropping_connection():
    store = Store()
    store.record("first", "apple", 1)
    version = store.build(0, 2)
    http_server = server(Autocomplete(store))
    thread = threading.Thread(
        target=http_server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{http_server.server_port}/suggest?prefix=a"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def status(suffix=""):
            try:
                with opener.open(endpoint + suffix, timeout=2) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                error.close()
                return error.code

        assert status() == 200
        assert status(f"&version={version}") == 200
        assert status("&version=9223372036854775808") == 400
        assert status("&version=9223372036854775807") == 503
    finally:
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=3)
        store.close()
        assert not thread.is_alive()


def test_empty_file_path_is_rejected_but_default_memory_store_is_shared():
    with pytest.raises(ValueError):
        invalid = Store("")
        invalid.close()
    store = Store()
    try:
        store.record("first", "apple", 1)
        assert store.aggregation(store.aggregate(0, 2))["frequencies"] == [["apple", 1]]
    finally:
        store.close()
