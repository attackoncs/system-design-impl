"""Coordinator ownership, failover and HTTP protocol regression coverage."""

import concurrent.futures
import contextlib
import http.server
import json
import threading
import time
import urllib.error
import urllib.request

import pytest
import search_autocomplete
import search_autocomplete.http as transport


@contextlib.contextmanager
def running(server):
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


@contextlib.contextmanager
def replica(callback):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            with contextlib.suppress(OSError):
                callback(self)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    with running(server) as endpoint:
        yield endpoint


def reply(handler, payload, status=200, **headers):
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Length", str(len(body)))
    for name, value in headers.items():
        handler.send_header(name.replace("_", "-"), value)
    handler.end_headers()
    handler.wfile.write(body)


def get(endpoint, query="prefix=a"):
    return urllib.request.urlopen(endpoint + "/suggest?" + query, timeout=2)


@pytest.fixture
def store():
    value = search_autocomplete.Store()
    value.record("one", "aa", 1)
    value.record("two", "az", 1)
    value.build(0, 2, shards=2)
    yield value
    value.close()


@pytest.mark.parametrize(
    "options",
    [
        {"timeout": True},
        {"timeout": float("nan")},
        {"timeout": float("inf")},
        {"timeout": 0},
        {"timeout": "1"},
        {"concurrency": True},
        {"concurrency": 1.2},
        {"concurrency": 0},
        {"request_timeout": True},
        {"request_timeout": float("nan")},
        {"request_timeout": float("inf")},
        {"request_timeout": 0},
    ],
)
def test_coordinator_bounds_are_finite_and_typed(store, options):
    with pytest.raises(ValueError):
        transport.Coordinator(store, {}, **options)


@pytest.mark.parametrize(
    "replicas",
    [
        None,
        [],
        {False: ["http://localhost"]},
        {0: []},
        {0: ["http://localhost:0"]},
        {0: ["http://localhost:invalid"]},
        {0: ["http://user:secret@localhost"]},
        {0: ["https://localhost?query=x"]},
        {0: ["https://localhost#fragment"]},
    ],
)
def test_coordinator_rejects_invalid_replica_configuration(store, replicas):
    with pytest.raises(ValueError):
        transport.Coordinator(store, replicas)


@pytest.mark.parametrize(
    ("field", "value"), [("version", True), ("revision", False), ("shard", False)]
)
def test_replica_bool_metadata_is_not_an_integer_generation(store, field, value):
    def invalid(handler):
        payload = {"version": 1, "revision": 0, "shard": 0, "suggestions": []}
        payload[field] = value
        reply(handler, payload)

    with (
        replica(invalid) as wrong,
        running(
            transport.server(search_autocomplete.Autocomplete(store), shard=0)
        ) as healthy,
    ):
        coordinator = transport.Coordinator(store, {0: [wrong, healthy]}, timeout=0.2)
        try:
            assert coordinator.suggest("aa")[2] == (("aa", 1),)
        finally:
            coordinator.close()


def test_candidate_must_belong_to_its_lexical_shard(store):
    def invalid(handler):
        reply(
            handler,
            {
                "version": 1,
                "revision": 0,
                "shard": 0,
                "suggestions": [{"query": "az", "frequency": 100}],
            },
        )

    with (
        replica(invalid) as wrong,
        running(
            transport.server(search_autocomplete.Autocomplete(store), shard=0)
        ) as first,
        running(
            transport.server(search_autocomplete.Autocomplete(store), shard=1)
        ) as second,
    ):
        coordinator = transport.Coordinator(
            store, {0: [wrong, first], 1: [second]}, timeout=0.2
        )
        try:
            assert coordinator.suggest("a")[2] == (("aa", 1), ("az", 1))
        finally:
            coordinator.close()


def test_replica_redirect_is_not_followed(store):
    calls = []

    def target(handler):
        calls.append(handler.path)
        reply(handler, {})

    with replica(target) as destination:

        def redirect(handler):
            reply(handler, {}, status=302, Location=destination + handler.path)

        with replica(redirect) as endpoint:
            coordinator = transport.Coordinator(store, {0: [endpoint]}, timeout=0.1)
            try:
                with pytest.raises(search_autocomplete.Unavailable):
                    coordinator.suggest("aa")
                assert not calls
            finally:
                coordinator.close()


def test_trickled_body_cannot_keep_resetting_replica_deadline(store):
    started = threading.Event()

    def trickle(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "1000")
        handler.end_headers()
        started.set()
        for _ in range(100):
            handler.wfile.write(b" ")
            handler.wfile.flush()
            time.sleep(0.01)

    with (
        replica(trickle) as slow,
        running(
            transport.server(search_autocomplete.Autocomplete(store), shard=0)
        ) as fast,
    ):
        coordinator = transport.Coordinator(
            store, {0: [slow, fast]}, timeout=0.1, request_timeout=0.2
        )
        try:
            before = time.monotonic()
            assert coordinator.suggest("aa")[2] == (("aa", 1),)
            assert started.is_set()
            assert time.monotonic() - before < 0.5
        finally:
            coordinator.close()


def test_oversized_replica_body_uses_valid_fallback(store):
    def huge(handler):
        reply(handler, "x" * 65537)

    with (
        replica(huge) as wrong,
        running(
            transport.server(search_autocomplete.Autocomplete(store), shard=0)
        ) as healthy,
    ):
        coordinator = transport.Coordinator(store, {0: [wrong, healthy]}, timeout=0.2)
        try:
            assert coordinator.suggest("aa")[2] == (("aa", 1),)
        finally:
            coordinator.close()


def test_trickled_headers_are_interrupted_and_close_drains(store):
    def trickle(handler):
        handler.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
        for _ in range(100):
            handler.connection.sendall(b"x")
            time.sleep(0.01)

    with (
        replica(trickle) as slow,
        running(
            transport.server(search_autocomplete.Autocomplete(store), shard=0)
        ) as fast,
    ):
        coordinator = transport.Coordinator(
            store, {0: [slow, fast]}, timeout=0.1, request_timeout=0.2
        )
        before = time.monotonic()
        try:
            assert coordinator.suggest("aa")[2] == (("aa", 1),)
        finally:
            coordinator.close()
        assert not coordinator._futures
        assert time.monotonic() - before < 0.5


def test_all_replica_attempts_share_one_request_deadline(store, monkeypatch):
    coordinator = transport.Coordinator(
        store, {0: ["http://127.0.0.1:1"] * 8}, timeout=0.2, request_timeout=0.12
    )
    attempts = []

    def expired(endpoint, params, deadline):
        attempts.append(endpoint)
        time.sleep(max(0, deadline - time.monotonic()))
        raise TimeoutError

    monkeypatch.setattr(coordinator, "_read_reply", expired)
    before = time.monotonic()
    try:
        with pytest.raises(search_autocomplete.Unavailable):
            coordinator.suggest("aa")
    finally:
        coordinator.close()
    assert len(attempts) == 8
    assert time.monotonic() - before < 0.5


def test_timeouts_keep_running_work_owned_and_bounded(store, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    coordinator = transport.Coordinator(
        store,
        {0: ["http://127.0.0.1:1"]},
        concurrency=1,
        timeout=0.05,
        request_timeout=0.05,
    )

    def slow(*_):
        calls.append(1)
        entered.set()
        assert release.wait(timeout=3)
        return {
            "version": 1,
            "revision": 0,
            "shard": 0,
            "suggestions": [{"query": "aa", "frequency": 1}],
        }

    monkeypatch.setattr(coordinator, "_read_reply", slow)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as callers:
            first = callers.submit(coordinator.suggest, "aa")
            assert entered.wait(timeout=1)
            with pytest.raises(search_autocomplete.Unavailable):
                first.result(timeout=1)
            attempts = [callers.submit(coordinator.suggest, "aa") for _ in range(12)]
            for attempt in attempts:
                with pytest.raises(search_autocomplete.Unavailable):
                    attempt.result(timeout=1)
        assert len(calls) == 1
        with coordinator.lock:
            assert len(coordinator._futures) == 1
        closing = threading.Thread(target=coordinator.close)
        closing.start()
        assert closing.is_alive()
        release.set()
        closing.join(timeout=2)
        assert not closing.is_alive()
        assert not coordinator._futures
        with pytest.raises(search_autocomplete.Unavailable):
            coordinator.suggest("aa")
    finally:
        release.set()
        coordinator.close()


def test_more_shards_than_workers_do_not_require_unbounded_submission(store):
    with (
        running(
            transport.server(search_autocomplete.Autocomplete(store), shard=0)
        ) as a,
        running(
            transport.server(search_autocomplete.Autocomplete(store), shard=1)
        ) as b,
    ):
        coordinator = transport.Coordinator(store, {0: [a], 1: [b]}, concurrency=1)
        try:
            assert coordinator.suggest("a")[2] == (("aa", 1), ("az", 1))
        finally:
            coordinator.close()


@pytest.mark.parametrize(
    "query",
    [
        "version=-1",
        "revision=%2B1",
        "shard=0.0",
        "version=1&version=1",
        "extra=x",
        "shard=1",
        "revision=",
        "version=%EF%BC%91",
    ],
)
def test_http_strict_request_validation(store, query):
    with running(
        transport.server(search_autocomplete.Autocomplete(store), shard=0)
    ) as endpoint:
        with pytest.raises(urllib.error.HTTPError) as error:
            get(endpoint, "prefix=aa&" + query)
        assert error.value.code == 400
        assert error.value.headers["Cache-Control"] == "no-store"


def test_generation_pinning_does_not_enable_stale_browser_cache(store):
    with running(
        transport.server(search_autocomplete.Autocomplete(store), shard=0)
    ) as endpoint:
        with get(endpoint, "prefix=aa&version=1&revision=0&shard=0") as response:
            assert response.headers["Cache-Control"] == "no-store"
            assert json.load(response)["suggestions"] == [
                {"query": "aa", "frequency": 1}
            ]
        store.block("aa")
        with pytest.raises(urllib.error.HTTPError) as error:
            get(endpoint, "prefix=aa&version=1&revision=0&shard=0")
        assert error.value.code == 503


def test_overload_returns_http_503_and_slot_recovers(store):
    server = transport.server(search_autocomplete.Autocomplete(store), shard=0)
    server.slots = threading.BoundedSemaphore(1)
    with running(server) as endpoint:
        assert server.slots.acquire(blocking=False)
        try:
            with pytest.raises(urllib.error.HTTPError) as error:
                get(endpoint, "prefix=aa")
            assert error.value.code == 503
        finally:
            server.slots.release()
        with get(endpoint, "prefix=aa") as response:
            assert response.status == 200


def test_coordinator_rejects_ignored_pinned_parameters(store):
    coordinator = transport.Coordinator(store, {})
    try:
        with running(transport.server(coordinator)) as endpoint:
            with pytest.raises(urllib.error.HTTPError) as error:
                get(endpoint, "prefix=aa&version=1")
            assert error.value.code == 400
    finally:
        coordinator.close()


def test_no_active_snapshot_is_http_unavailable():
    store = search_autocomplete.Store()
    coordinator = transport.Coordinator(store, {})
    try:
        with running(transport.server(coordinator)) as endpoint:
            with pytest.raises(urllib.error.HTTPError) as error:
                get(endpoint)
            assert error.value.code == 503
    finally:
        coordinator.close()
        store.close()


def test_server_close_drains_active_service_handlers(store, monkeypatch):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    service = search_autocomplete.Autocomplete(store)
    original = service.suggest

    def waiting(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "suggest", waiting)
    server = transport.server(service, shard=0)
    serving = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    serving.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"

    def close():
        server.server_close()
        closed.set()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as caller:
            pending = caller.submit(get, endpoint, "prefix=aa")
            assert entered.wait(timeout=1)
            server.shutdown()
            closing = threading.Thread(target=close)
            closing.start()
            assert not closed.wait(timeout=0.05)
            release.set()
            with pending.result(timeout=2) as response:
                assert json.load(response)["suggestions"] == [
                    {"query": "aa", "frequency": 1}
                ]
            closing.join(timeout=2)
            assert closed.is_set()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)
