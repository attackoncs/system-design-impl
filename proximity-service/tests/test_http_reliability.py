"""Owned HTTP I/O, strict framing, region failover and bounded admission."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr
from http.client import HTTPException, HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from proximity.http import Replicator, Router, fetch, server
from proximity.store import Error, Store


TOKENS = {
    "owners": {"alice-token": "alice"},
    "admin": "admin-token",
    "replication": "replication-token",
}


@contextmanager
def running(http):
    thread = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield http, f"http://127.0.0.1:{http.server_address[1]}"
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


@contextmanager
def upstream(callback):
    stopped = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            try:
                callback(self, stopped)
            except OSError:
                pass

    class OwnedServer(ThreadingHTTPServer):
        daemon_threads = False

    http = OwnedServer(("127.0.0.1", 0), Handler)
    with running(http) as result:
        try:
            yield result
        finally:
            stopped.set()


def reply(handler, value=None, status=200):
    data = json.dumps({"ok": True} if value is None else value).encode()
    handler.send_response(status)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def raw(http, target, headers=(), body=b"", method="GET"):
    with socket.create_connection(http.server_address, timeout=2) as stream:
        data = f"{method} {target} HTTP/1.0\r\nHost: localhost\r\n".encode()
        data += b"".join(
            (name + ": " + value + "\r\n").encode() for name, value in headers
        )
        stream.sendall(data + b"\r\n" + body)
        response = HTTPResponse(stream)
        response.begin()
        return response.status, json.loads(
            response.read()
        ) if response.length != 0 else None


def get(url, path="/health"):
    return urlopen(Request(url + path), timeout=3)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="proximity-http-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "metadata.sqlite3")

    def test_ambiguous_request_framing(self):
        cases = [
            [("Content-Length", "0"), ("Content-Length", "0")],
            [("Content-Length", "0"), ("Content-Length", "1")],
            [("Transfer-Encoding", "chunked")],
            [("Content-Length", "+0")],
            [("Content-Length", "-1")],
            [("Content-Type", "application/json"), ("Content-Type", "text/plain")],
        ]
        with running(server("127.0.0.1", 0, store=self.store, tokens=TOKENS)) as (
            http,
            _url,
        ):
            for headers in cases:
                with self.subTest(headers=headers):
                    status, _body = raw(
                        http,
                        "/v1/businesses",
                        [("Authorization", "Bearer alice-token"), *headers],
                        method="POST",
                    )
                    self.assertEqual(status, 400)

    def test_authentication_precedes_body_read(self):
        with running(server("127.0.0.1", 0, store=self.store, tokens=TOKENS)) as (
            http,
            _url,
        ):
            started = time.monotonic()
            status, _ = raw(
                http,
                "/v1/businesses",
                [("Authorization", "Bearer invalid"), ("Content-Length", "100")],
                method="POST",
            )
            self.assertEqual(status, 403)
            self.assertLess(time.monotonic() - started, 1)

    def test_duplicate_auth_and_query_validation(self):
        with running(server("127.0.0.1", 0, store=self.store, tokens=TOKENS)) as (
            http,
            _url,
        ):
            status, _ = raw(
                http,
                "/internal/snapshot",
                [("Authorization", "Bearer replication-token")] * 2,
            )
            self.assertEqual(status, 400)
            for target in (
                "/health?unknown=1",
                "/health?x=1&x=2",
                "/health?x",
                "/health?%ZZ=1",
                "http://elsewhere.invalid/health",
                "/health#fragment",
                "/v1/search/nearby?latitude=0&longitude=0&radius=-1",
                "/v1/search/nearby?latitude=0&longitude=0&limit=%EF%BC%91",
                "/v1/search/nearby?latitude=0&longitude=0&category=",
            ):
                with self.subTest(target=target):
                    self.assertEqual(raw(http, target)[0], 400)

    def test_duplicate_nonfinite_and_nonobject_json(self):
        with running(server("127.0.0.1", 0, store=self.store, tokens=TOKENS)) as (
            http,
            _url,
        ):
            for data in (
                b'{"name":"a","name":"b"}',
                b'{"name":NaN}',
                b'{"latitude":1e999}',
                b"[]",
            ):
                with self.subTest(data=data):
                    status, _ = raw(
                        http,
                        "/v1/businesses",
                        [
                            ("Authorization", "Bearer alice-token"),
                            ("Idempotency-Key", "create"),
                            ("Content-Length", str(len(data))),
                        ],
                        data,
                        "POST",
                    )
                    self.assertEqual(status, 400)

    def test_publication_route_is_bodyless_and_diagnostic_is_visible(self):
        http = server("127.0.0.1", 0, store=self.store, tokens=TOKENS)
        http.publisher = type("Publisher", (), {"last_error": "publication failed"})()
        with running(http) as (http, url):
            status, _ = raw(
                http,
                "/internal/publish",
                [("Authorization", "Bearer admin-token"), ("Content-Length", "2")],
                b"{}",
                "POST",
            )
            self.assertEqual(status, 400)
            with get(url) as response:
                self.assertEqual(
                    json.load(response)["publication_error"], "publication failed"
                )
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_total_capacity_overload_and_recovery(self):
        with running(server("127.0.0.1", 0, store=self.store, concurrency=1)) as (
            http,
            url,
        ):
            self.assertTrue(http.slots.acquire(False))
            try:
                self.assertEqual(raw(http, "/health")[0], 503)
            finally:
                http.slots.release()
            with get(url) as response:
                self.assertEqual(response.status, 200)

    def _slow_socket(self, body=False):
        http = server(
            "127.0.0.1",
            0,
            store=self.store,
            tokens=TOKENS,
            concurrency=1,
            request_timeout=0.15,
        )
        with running(http) as (http, url):
            with socket.create_connection(http.server_address, timeout=2) as stream:
                if body:
                    stream.sendall(
                        b"POST /v1/businesses HTTP/1.0\r\nAuthorization: Bearer alice-token\r\nContent-Length: 100\r\n\r\n"
                    )
                else:
                    stream.sendall(b"GET /health HTTP/1.0\r\nX-Slow: ")
                end = time.monotonic() + 1
                while time.monotonic() < end:
                    try:
                        stream.sendall(b"x")
                        time.sleep(0.03)
                    except OSError:
                        break
                self.assertLess(time.monotonic(), end)
            self.assertTrue(http.slots.acquire(timeout=1))
            http.slots.release()
            with get(url) as response:
                self.assertEqual(response.status, 200)

    def test_deadline_interrupts_slow_headers(self):
        self._slow_socket()

    def test_deadline_interrupts_slow_body(self):
        self._slow_socket(body=True)

    def test_server_close_drains_accepted_store_work(self):
        entered, release, closed = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        original = self.store.health

        def slow():
            entered.set()
            self.assertTrue(release.wait(2))
            return original()

        with patch.object(self.store, "health", slow):
            with (
                running(server("127.0.0.1", 0, store=self.store)) as (http, url),
                ThreadPoolExecutor(2) as pool,
            ):
                pending = pool.submit(get, url)
                self.assertTrue(entered.wait(1))
                http.shutdown()

                def close():
                    http.server_close()
                    closed.set()

                closing = pool.submit(close)
                try:
                    self.assertFalse(closed.wait(0.05))
                finally:
                    release.set()
                closing.result(2)
                with self.assertRaises(OSError):
                    pending.result(2)

    def test_error_boundary_does_not_log_location_or_credentials(self):
        captured = io.StringIO()
        with patch.object(
            self.store,
            "search",
            side_effect=RuntimeError("private latitude 12.345678901"),
        ):
            with (
                redirect_stderr(captured),
                running(server("127.0.0.1", 0, store=self.store)) as (http, _url),
            ):
                status, body = raw(
                    http, "/v1/search/nearby?latitude=12.345678901&longitude=0"
                )
                self.assertEqual(status, 500)
                self.assertNotIn("12.345678901", json.dumps(body))
        self.assertEqual(captured.getvalue(), "")

    def test_invalid_http_and_auth_configuration(self):
        for kwargs in (
            {"concurrency": True},
            {"concurrency": 0},
            {"request_timeout": float("nan")},
            {"request_timeout": True},
            {"tokens": {"admin": None}},
            {"tokens": {"owners": {"unicode-é": "alice"}}},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                server("127.0.0.1", 0, store=self.store, **kwargs)


class OutboundTests(unittest.TestCase):
    def test_fetch_rejects_redirect_without_forwarding_credential(self):
        received = []
        with upstream(
            lambda h, _stop: (received.append(h.headers.get("Authorization")), reply(h))
        ) as (_target, target):

            def redirect(handler, _stop):
                handler.send_response(302)
                handler.send_header("Location", target + "/stolen")
                handler.send_header("Content-Length", "0")
                handler.end_headers()

            with upstream(redirect) as (_source, source):
                with self.assertRaises(HTTPError) as error:
                    fetch(source, "replication-token")
                self.assertEqual(error.exception.code, 302)
        self.assertEqual(received, [])

    def test_fetch_size_duplicate_fields_and_nonfinite_numbers(self):
        for data, maximum in (
            (b'{"a":1,"a":2}', 100),
            (b'{"a":1e999}', 100),
            (b'{"a":NaN}', 100),
            (b'"oversized"', 4),
        ):

            def response(handler, _stop):
                handler.send_response(200)
                handler.send_header("Content-Length", str(len(data)))
                handler.end_headers()
                handler.wfile.write(data)

            with self.subTest(data=data), upstream(response) as (_http, url):
                with self.assertRaises(ValueError):
                    fetch(url, max_bytes=maximum)

    def test_fetch_rejects_truncated_or_ambiguous_response_framing(self):
        cases = (
            (("Content-Length", "100"),),
            (("Content-Length", "2"), ("Content-Length", "2")),
            (("Content-Length", "-2"),),
            (("Content-Length", "+2"),),
            (("Content-Length", "2"), ("Transfer-Encoding", "chunked")),
            (("Transfer-Encoding", "gzip"),),
        )
        for headers in cases:

            def response(handler, _stop):
                handler.send_response(200)
                for name, value in headers:
                    handler.send_header(name, value)
                handler.end_headers()
                handler.wfile.write(b"{}")

            with self.subTest(headers=headers), upstream(response) as (_http, url):
                with self.assertRaises(ValueError):
                    fetch(url)

    def test_fetch_accepts_valid_chunked_response(self):
        def response(handler, _stop):
            handler.send_response(200)
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            handler.wfile.write(b"2\r\n{}\r\n0\r\n\r\n")

        with upstream(response) as (_http, url):
            self.assertEqual(fetch(url), {})

    def _slow(self, handler, stopped, body=False, entered=None):
        if entered is not None:
            entered.set()
        if body:
            handler.send_response(200)
            handler.send_header("Content-Length", "1000")
            handler.end_headers()
        else:
            handler.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
        while not stopped.wait(0.02):
            handler.connection.sendall(b" ")

    def test_fetch_absolute_deadline_covers_slow_headers_and_body(self):
        for body in (False, True):
            with (
                self.subTest(body=body),
                upstream(lambda h, stop: self._slow(h, stop, body)) as (_http, url),
            ):
                started = time.monotonic()
                with self.assertRaises((OSError, HTTPException)):
                    fetch(url, timeout=0.15)
                self.assertLess(time.monotonic() - started, 0.8)

    def test_router_uses_one_budget_and_preserves_time_for_failover(self):
        with (
            upstream(self._slow) as (_slow_http, slow),
            upstream(lambda h, _stop: reply(h)) as (_fast_http, fast),
        ):
            router = Router({"local": [slow, fast]}, timeout=0.5, request_timeout=0.25)
            started = time.monotonic()
            self.assertTrue(
                router.read("local", "/v1/search/nearby?latitude=0&longitude=0")["ok"]
            )
            self.assertLess(time.monotonic() - started, 0.6)

    def test_router_does_not_retry_4xx_or_cross_regions(self):
        calls = []
        for code in (400, 404, 409):
            with self.subTest(code=code):
                with (
                    upstream(lambda h, _stop: reply(h, status=code)) as (
                        _bad_http,
                        bad,
                    ),
                    upstream(lambda h, _stop: (calls.append(1), reply(h))) as (
                        _other_http,
                        other,
                    ),
                ):
                    router = Router({"local": [bad, other], "remote": [other]})
                    with self.assertRaises(Error) as error:
                        router.read("local", "/v1/businesses/test")
                    self.assertEqual(error.exception.status, code)
        self.assertEqual(calls, [])

    def test_router_retries_5xx_and_redirect_as_invalid_replica(self):
        for code in (302, 500, 503):
            with self.subTest(code=code):
                with (
                    upstream(lambda h, _stop: reply(h, status=code)) as (
                        _bad_http,
                        bad,
                    ),
                    upstream(lambda h, _stop: reply(h)) as (_good_http, good),
                ):
                    self.assertTrue(
                        Router({"local": [bad, good]}).read(
                            "local", "/v1/businesses/test"
                        )["ok"]
                    )

    def test_router_admission_is_bounded_while_slow_work_owns_slot(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def blocked(*_args, **_kwargs):
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(2))
            return {"ok": True}

        router = Router(
            {"local": ["http://unused.test"]}, concurrency=1, request_timeout=0.1
        )
        with patch("proximity.http.fetch", blocked), ThreadPoolExecutor(1) as pool:
            pending = pool.submit(router.read, "local", "/v1/businesses/test")
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaises(Error) as error:
                    router.read("local", "/v1/businesses/test")
                self.assertEqual(error.exception.status, 503)
            finally:
                release.set()
            with self.assertRaises(Error):
                pending.result(2)
        self.assertEqual(len(calls), 1)

    def test_region_map_input_is_detached_and_invalid_endpoints_rejected(self):
        regions = {"local": ["http://127.0.0.1:1"]}
        router = Router(regions)
        regions["local"][0] = "http://elsewhere.invalid"
        self.assertEqual(router.regions["local"], ("http://127.0.0.1:1",))
        for regions in (
            {"local": []},
            {"local": ["http://user:password@localhost"]},
            {"local": ["http://localhost:0"]},
            {"local": ["http://localhost?query=1"]},
            {"local": ["http://localhost"] * 9},
        ):
            with self.subTest(regions=regions), self.assertRaises(ValueError):
                Router(regions)

    def test_replicator_close_cancels_and_joins_active_transfer(self):
        entered, imported = threading.Event(), []
        store = type(
            "Replica",
            (),
            {"import_snapshot": lambda _self, value: imported.append(value)},
        )()
        with upstream(
            lambda h, stop: self._slow(h, stop, body=True, entered=entered)
        ) as (_http, url):
            replicator = Replicator(store, url, "replication-token").start()
            try:
                self.assertTrue(entered.wait(1))
                started = time.monotonic()
                replicator.close()
                self.assertFalse(replicator.thread.is_alive())
                self.assertLess(time.monotonic() - started, 1)
            finally:
                replicator.close()
        self.assertEqual(imported, [])

    def test_replicator_validation_and_no_restart_after_close(self):
        store = object()
        for kwargs in (
            {"interval": float("nan")},
            {"interval": True},
            {"interval": -1},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Replicator(store, "http://localhost", "replication-token", **kwargs)
        replicator = Replicator(store, "http://localhost", "replication-token")
        replicator.close()
        with self.assertRaises(RuntimeError):
            replicator.start()


if __name__ == "__main__":
    unittest.main()
