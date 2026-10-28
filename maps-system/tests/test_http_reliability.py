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
from maps_service.http import Replicator, Gateway, fetch, server
from maps_service.store import Error, Store


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

        do_POST = do_GET

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
        self.temp = tempfile.TemporaryDirectory(prefix="maps-http-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "metadata.sqlite3")
        self.store.import_graph(json.loads((ROOT / "examples/roads.json").read_text()))
        self.store.provision("alice", "alice-token")
        self.store.provision("admin", "admin-token", admin=True)

    def test_ambiguous_framing_and_unknown_queries(self):
        with running(server("127.0.0.1", 0, store=self.store)) as (http, _):
            for headers in (
                [("Content-Length", "0"), ("Content-Length", "0")],
                [("Transfer-Encoding", "chunked")],
                [("Content-Length", "+0")],
                [("Content-Length", "-1")],
                [("Content-Type", "application/json"), ("Content-Type", "text/plain")],
            ):
                with self.subTest(headers=headers):
                    self.assertEqual(
                        raw(http, "/v1/locations", headers, method="POST")[0], 400
                    )
            for target in (
                "/health?unknown=1",
                "/health?x=1&x=2",
                "/health?x",
                "/health?%ZZ=1",
                "http://elsewhere.invalid/health",
                "/health#bad",
                "/v1/reverse?latitude=0&longitude=0&extra=1",
            ):
                with self.subTest(target=target):
                    self.assertEqual(raw(http, target)[0], 400)
            self.assertEqual(
                raw(
                    http, "/v1/locations", [("Authorization", "Bearer alice-token")] * 2
                )[0],
                400,
            )

    def test_authentication_precedes_body_read(self):
        with running(server("127.0.0.1", 0, store=self.store)) as (http, _):
            for target in ("/v1/locations", "/internal/graph", "/internal/traffic"):
                started = time.monotonic()
                self.assertEqual(
                    raw(http, target, [("Content-Length", "100")], method="POST")[0],
                    401,
                )
                self.assertLess(time.monotonic() - started, 1)

    def test_strict_json_and_body_media_type(self):
        with running(server("127.0.0.1", 0, store=self.store)) as (http, _):
            for data in (
                b'{"records":[],"records":[]}',
                b'{"ttl":NaN}',
                b'{"ttl":1e999}',
                b"[]",
            ):
                with self.subTest(data=data):
                    headers = [
                        ("Authorization", "Bearer admin-token"),
                        ("Content-Length", str(len(data))),
                    ]
                    self.assertEqual(
                        raw(http, "/internal/traffic", headers, data, "POST")[0], 400
                    )
            self.assertEqual(
                raw(
                    http,
                    "/v1/route",
                    [("Content-Length", "2"), ("Content-Type", "text/plain")],
                    b"{}",
                    "POST",
                )[0],
                415,
            )

    def test_conditional_tile_lists_and_weak_etag(self):
        with running(server("127.0.0.1", 0, store=self.store)) as (http, url):
            graph = self.store.health()["graph"]
            path = f"/v1/tiles/{graph}/0/0/0.png"
            with get(url, path) as response:
                etag = response.headers["ETag"]
                self.assertEqual(response.read()[:8], b"\x89PNG\r\n\x1a\n")
            for condition in ("*", "W/" + etag, '"other", ' + etag):
                with self.subTest(condition=condition):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(
                            Request(url + path, headers={"If-None-Match": condition}),
                            timeout=2,
                        )
                    self.assertEqual(error.exception.code, 304)
                    self.assertEqual(error.exception.headers["ETag"], etag)
                    self.assertIsNone(error.exception.headers.get("Content-Length"))
                    error.exception.close()

    def test_new_public_tile_urls_and_hierarchical_routing_routes(self):
        with running(server("127.0.0.1", 0, store=self.store)) as (_, url):
            with get(url, "/v1/tile-urls?latitude=0&longitude=0&zoom=0") as response:
                value = json.load(response)
                self.assertEqual(len(value["tiles"]), 1)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            graph = value["graph"]
            for path in (
                f"/v1/routing-tiles/{graph}/0/0",
                f"/v1/routing-tiles/{graph}/8/0/0",
            ):
                with get(url, path) as response:
                    self.assertEqual(json.load(response)["graph"], graph)

    def test_capacity_overload_and_recovery(self):
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

    def _slow_socket(self, body):
        with running(
            server(
                "127.0.0.1", 0, store=self.store, concurrency=1, request_timeout=0.15
            )
        ) as (http, url):
            with socket.create_connection(http.server_address, timeout=2) as stream:
                stream.sendall(
                    b"POST /v1/route HTTP/1.0\r\nContent-Length: 100\r\n\r\n"
                    if body
                    else b"GET /health HTTP/1.0\r\nX-Slow: "
                )
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

    def test_slow_headers_have_an_absolute_deadline(self):
        self._slow_socket(False)

    def test_slow_body_has_an_absolute_deadline(self):
        self._slow_socket(True)

    def test_server_close_waits_for_accepted_store_work(self):
        entered, release, closed = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        original = self.store.health

        def slow():
            entered.set()
            self.assertTrue(release.wait(3))
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

    def test_error_boundary_does_not_log_credentials_or_coordinates(self):
        captured = io.StringIO()
        with patch.object(
            self.store, "geocode", side_effect=RuntimeError("private 12.345678901")
        ):
            with (
                redirect_stderr(captured),
                running(server("127.0.0.1", 0, store=self.store)) as (http, _),
            ):
                status, body = raw(
                    http,
                    "/v1/reverse?latitude=12.345678901&longitude=0",
                    [("Authorization", "Bearer secret-value")],
                )
                self.assertEqual(status, 500)
                self.assertNotIn("12.345678901", json.dumps(body))
        self.assertEqual(captured.getvalue(), "")

    def test_http_configuration_is_strict(self):
        for kwargs in (
            {"concurrency": True},
            {"concurrency": 0},
            {"request_timeout": float("nan")},
            {"request_timeout": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                server("127.0.0.1", 0, store=self.store, **kwargs)

    def test_private_navigation_auth_fields_and_finite_sse_pages(self):
        class Tracker:
            def create(inner, actor, body):
                self.assertEqual(actor, "alice")
                return {"id": "session1", "origin": body["origin"]}

            def read(inner, actor, identifier):
                return {"id": identifier, "actor": actor}

            def move(inner, actor, identifier, origin):
                return {"id": identifier, "origin": origin}

            def remove(inner, actor, identifier):
                return {"removed": identifier}

            def events(inner, actor, identifier, **options):
                self.assertEqual(
                    (actor, identifier, options["after"]), ("alice", "session1", 2)
                )
                self.assertLessEqual(options["wait"], 5)
                return [{"id": 3, "event": "route", "data": {"revision": 3}}]

        with running(server("127.0.0.1", 0, store=self.store, tracker=Tracker())) as (
            http,
            url,
        ):
            self.assertEqual(
                raw(http, "/v1/navigation", [("Content-Length", "100")], method="POST")[
                    0
                ],
                401,
            )
            auth = [("Authorization", "Bearer alice-token")]
            self.assertEqual(
                raw(http, "/v1/navigation/session1", auth)[1]["actor"], "alice"
            )
            self.assertEqual(
                raw(http, "/v1/navigation/session1", auth, method="DELETE")[1][
                    "removed"
                ],
                "session1",
            )
            with urlopen(
                Request(
                    url + "/v1/navigation/session1/events?after=2",
                    headers={
                        "Authorization": "Bearer alice-token",
                        "Last-Event-ID": "2",
                    },
                ),
                timeout=2,
            ) as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertTrue(
                    response.headers["Content-Type"].startswith("text/event-stream")
                )
                self.assertEqual(
                    response.read(), b'id: 3\nevent: route\ndata: {"revision":3}\n\n'
                )
            for path, headers in (
                (
                    "/v1/navigation/session1/events?after=2",
                    [*auth, ("Last-Event-ID", "1")],
                ),
                ("/v1/navigation/session1/events?after=-1", auth),
                ("/v1/navigation/session1/events?after=2&other=1", auth),
            ):
                self.assertEqual(raw(http, path, headers)[0], 400)

    def test_event_wait_capacity_preserves_foreground_and_releases_after_response(self):
        entered, release = threading.Event(), threading.Event()

        class Tracker:
            def events(inner, actor, identifier, **options):
                entered.set()
                self.assertTrue(release.wait(2))
                return []

        with (
            running(
                server(
                    "127.0.0.1", 0, store=self.store, tracker=Tracker(), concurrency=2
                )
            ) as (http, url),
            ThreadPoolExecutor(1) as pool,
        ):

            def wait():
                with urlopen(
                    Request(
                        url + "/v1/navigation/session1/events",
                        headers={"Authorization": "Bearer alice-token"},
                    ),
                    timeout=3,
                ) as response:
                    return response.read()

            pending = pool.submit(wait)
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(
                    raw(
                        http,
                        "/v1/navigation/session1/events",
                        [("Authorization", "Bearer alice-token")],
                    )[0],
                    503,
                )
                with get(url) as response:
                    self.assertEqual(response.status, 200)
            finally:
                release.set()
            self.assertEqual(pending.result(2), b": keep-alive\n\n")
            self.assertTrue(http.events.acquire(timeout=1))
            http.events.release()


class OutboundTests(unittest.TestCase):
    def test_fetch_rejects_redirect_without_forwarding_credential(self):
        received = []
        with upstream(
            lambda h, _: (received.append(h.headers.get("Authorization")), reply(h))
        ) as (_, target):

            def redirect(h, _):
                h.send_response(302)
                h.send_header("Location", target + "/stolen")
                h.send_header("Content-Length", "0")
                h.end_headers()

            with upstream(redirect) as (_, source):
                with self.assertRaises(HTTPError) as error:
                    fetch(source, token="replication-token")
                self.assertEqual(error.exception.code, 302)
        self.assertEqual(received, [])

    def test_fetch_rejects_truncation_ambiguous_framing_and_size(self):
        cases = [
            b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{}",
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n0\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n",
        ]
        for data in cases:

            def respond(h, _):
                h.connection.sendall(data)
                h.close_connection = True

            with self.subTest(data=data), upstream(respond) as (_, url):
                with self.assertRaises((ValueError, HTTPException)):
                    fetch(url, max_bytes=100)

    def test_fetch_accepts_valid_chunked_binary(self):
        def respond(h, _):
            h.connection.sendall(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: image/png\r\n\r\n3\r\nabc\r\n0\r\n\r\n"
            )

        with upstream(respond) as (_, url):
            self.assertEqual(fetch(url)[:2], (b"abc", "image/png"))

    def test_fetch_deadline_covers_slow_headers_and_body(self):
        for body in (False, True):

            def slow(h, stopped):
                h.connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n"
                    if body
                    else b"HTTP/1.1 200 OK\r\nX-Slow: "
                )
                while not stopped.wait(0.02):
                    h.connection.sendall(b"x")

            with self.subTest(body=body), upstream(slow) as (_, url):
                started = time.monotonic()
                with self.assertRaises((OSError, ValueError, HTTPException)):
                    fetch(url, timeout=0.15)
                self.assertLess(time.monotonic() - started, 0.7)

    def test_gateway_one_budget_failover_and_response_validation(self):
        observed = []

        def fake(url, method="GET", body=None, **kwargs):
            observed.append((url, kwargs["timeout"]))
            if "first" in url:
                time.sleep(kwargs["timeout"])
                raise TimeoutError()
            return b'{"ok":true}', "application/json", "unsafe-cache"

        gateway = Gateway(
            {"one": ["http://first", "http://second"]}, timeout=2, request_timeout=0.2
        )
        started = time.monotonic()
        with patch("maps_service.http.fetch", fake):
            self.assertEqual(
                gateway.read("one", "/v1/route", "POST", {})[-1], "no-store"
            )
        self.assertEqual(len(observed), 2)
        self.assertLess(time.monotonic() - started, 0.4)
        self.assertLessEqual(observed[0][1], 0.1)
        with patch(
            "maps_service.http.fetch",
            return_value=(b'{"a":1,"a":2}', "application/json", "no-store"),
        ):
            with self.assertRaises(Error) as error:
                gateway.read("one", "/v1/route", "POST", {})
            self.assertEqual(error.exception.status, 503)

    def test_gateway_does_not_cross_regions_retry_4xx_or_follow_redirect(self):
        gateway = Gateway(
            {"one": ["http://first", "http://second"], "other": ["http://elsewhere"]}
        )

        def fail(url, *args, **kwargs):
            raise HTTPError(url, 409, "changed", None, None)

        with patch("maps_service.http.fetch", side_effect=fail) as call:
            with self.assertRaises(Error) as error:
                gateway.read("one", "/v1/route", "POST", {})
            self.assertEqual(error.exception.status, 409)
            self.assertEqual(call.call_count, 1)
        with patch(
            "maps_service.http.fetch",
            side_effect=lambda url, *args, **kwargs: (_ for _ in ()).throw(
                HTTPError(url, 302, "redirect", None, None)
            ),
        ) as call:
            with self.assertRaises(Error) as error:
                gateway.read("one", "/v1/route", "POST", {})
            self.assertEqual(error.exception.status, 503)
            self.assertTrue(
                all("elsewhere" not in c.args[0] for c in call.call_args_list)
            )

    def test_gateway_capacity_held_until_work_exits(self):
        entered, release = threading.Event(), threading.Event()
        gateway = Gateway({"one": ["http://first"]}, concurrency=1, request_timeout=0.1)

        def slow(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return b"{}", "application/json", "no-store"

        with patch("maps_service.http.fetch", slow), ThreadPoolExecutor(2) as pool:
            first = pool.submit(gateway.read, "one", "/v1/route", "POST", {})
            self.assertTrue(entered.wait(1))
            try:
                with self.assertRaises(Error) as error:
                    gateway.read("one", "/v1/route", "POST", {})
                self.assertEqual(error.exception.status, 503)
            finally:
                release.set()
            with self.assertRaises(Error):
                first.result(2)

    def test_gateway_copies_regions_and_validates_configuration(self):
        config = {"one": ["http://first"]}
        gateway = Gateway(config)
        config["one"].append("http://untrusted")
        self.assertEqual(gateway.regions["one"], ("http://first",))
        for config in (
            {},
            {"one": []},
            {"one": ["ftp://host"]},
            {"one": ["http://user:pass@host"]},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                Gateway(config)

    def test_replicator_close_cancels_and_joins_transfer(self):
        entered = threading.Event()

        class StoreStub:
            def install_snapshot(self, value):
                self.fail = value

        store = StoreStub()

        def slow(h, stopped):
            entered.set()
            h.connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
            while not stopped.wait(0.02):
                h.connection.sendall(b" ")

        with upstream(slow) as (_, url):
            replicator = Replicator(store, url, "private-token", interval=1).start()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            replicator.close()
            self.assertFalse(replicator.thread.is_alive())
            self.assertLess(time.monotonic() - started, 0.7)
        self.assertFalse(hasattr(store, "fail"))
        with self.assertRaises(RuntimeError):
            replicator.start()

    def test_replicator_recovers_after_bad_json_and_rejects_config(self):
        class StoreStub:
            def install_snapshot(self, value):
                self.value = value
                replicator.stop.set()

        store = StoreStub()
        replicator = Replicator(store, "http://host", "private-token", interval=0.001)

        def response(*args, **kwargs):
            if not hasattr(response, "called"):
                response.called = True
                return b'{"version":1,"version":2}', "application/json", "no-store"
            return b"{}", "application/json", "no-store"

        with patch("maps_service.http.fetch", response):
            replicator.run()
        self.assertEqual(store.value, {})
        self.assertIsNone(replicator.error)
        for kwargs in (
            {"interval": 0},
            {"interval": True},
            {"interval": float("nan")},
            {"token": "bad\nvalue"},
        ):
            values = {"interval": 1, "token": "private-token", **kwargs}
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Replicator(store, "http://host", **values)


if __name__ == "__main__":
    unittest.main()
