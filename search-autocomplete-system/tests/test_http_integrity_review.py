"""A complete JSON prefix must not hide an incomplete HTTP response body."""

from contextlib import contextmanager
import http.server
import json
import threading

import pytest

from search_autocomplete.http import Coordinator
from search_autocomplete.store import Store, Unavailable


@contextmanager
def replica(payload, extra_length=0, include_length=True):
    body = json.dumps(payload).encode("utf-8")

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            if include_length:
                self.send_header("Content-Length", str(len(body) + extra_length))
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

    with http.server.HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
        )
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=2)
            assert not thread.is_alive()


@pytest.fixture
def store():
    value = Store()
    value.record("event", "alpha", 100)
    value.build(0, 200, shards=1)
    try:
        yield value
    finally:
        value.close()


def reply(store, frequency):
    version, revision, _ = store.view()
    return {
        "version": version,
        "revision": revision,
        "shard": 0,
        "suggestions": [{"query": "alpha", "frequency": frequency}],
    }


@pytest.mark.parametrize("extra_length", [0, 17])
def test_content_length_is_enforced_even_for_valid_json(store, extra_length):
    with replica(reply(store, 9), extra_length) as endpoint:
        coordinator = Coordinator(store, {0: [endpoint]})
        try:
            if extra_length:
                with pytest.raises(Unavailable):
                    coordinator.suggest("a")
            else:
                assert coordinator.suggest("a")[2] == (("alpha", 9),)
        finally:
            coordinator.close()


def test_truncated_replica_falls_back_to_complete_replica(store):
    with replica(reply(store, 99), 17) as broken, replica(reply(store, 1)) as healthy:
        coordinator = Coordinator(store, {0: [broken, healthy]})
        try:
            assert coordinator.suggest("a")[2] == (("alpha", 1),)
        finally:
            coordinator.close()


def test_close_delimited_http_body_remains_supported(store):
    with replica(reply(store, 1), include_length=False) as endpoint:
        coordinator = Coordinator(store, {0: [endpoint]})
        try:
            assert coordinator.suggest("a")[2] == (("alpha", 1),)
        finally:
            coordinator.close()


@pytest.mark.parametrize("name", ["timeout", "request_timeout"])
def test_huge_timeout_has_configuration_validation_error(store, name):
    with pytest.raises(ValueError):
        Coordinator(store, {}, **{name: 10**1000})
