"""HTTP contracts and fault peers; these do not replace real Redis integration."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from gaming_leaderboard.common import Error, http_json
from gaming_leaderboard.http import server
from gaming_leaderboard.transport import loads

CONFIG = {
    kind + "_token": char * 48
    for kind, char in (("admin", "a"), ("read", "r"), ("game", "g"), ("internal", "i"))
}
CONFIG["projection_urls"] = []


@contextmanager
def running(instance):
    thread = threading.Thread(
        target=instance.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join()


def raw_request(instance, raw):
    with socket.create_connection(instance.server_address, timeout=2) as sock:
        sock.sendall(raw)
        sock.shutdown(socket.SHUT_WR)
        result = bytearray()
        while True:
            part = sock.recv(65536)
            if not part:
                return bytes(result)
            result.extend(part)


class FakeService:
    def __init__(self):
        self.in_operation = self.in_guard = False
        self.last_deadline = self.last_options = None
        self.journal = SimpleNamespace(
            status=lambda: {"heads": {"0": 1}},
            feed=lambda shard, after: {"shard": shard, "after": after},
            register=lambda user, name: {"user": user, "name": name},
            ingest=lambda body: {"seq": 1},
            close=lambda month: {"month": month},
        )

    @contextmanager
    def operation(self, *, deadline, cancel):
        self.in_operation = True
        self.last_deadline = deadline
        try:
            yield
        finally:
            self.in_operation = False

    @contextmanager
    def guard(self, method, path):
        self.in_guard = True
        try:
            yield
        finally:
            self.in_guard = False

    def metrics(self):
        return {"pool": "owned"}

    def query(self, month=None, user=None, radius=0, **options):
        if isinstance(month, dict):
            self.last_options = options
            return {"projection": True}
        assert self.in_operation and self.in_guard
        return {"month": month, "user": user, "radius": radius}

    def season(self, month):
        return {"month": month}

    def reconcile(self, month):
        return {"month": month, "remaining": self.last_deadline - time.monotonic()}

    def ready(self, heads, timeout=1):
        return False

    def status(self, **options):
        self.last_options = options
        return {"shard": 0}

    def reset(self, **options):
        self.last_options = options
        return {"rebuilding": True}


class HTTPReliabilityTests(unittest.TestCase):
    def raw(self, request, *, role="authority", store=None):
        with running(
            server(role, CONFIG, store or FakeService(), "127.0.0.1", 0)
        ) as instance:
            return raw_request(instance, request)

    def get(self, path, *, token="read"):
        return self.raw(
            f"GET {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {CONFIG[token + '_token']}\r\n\r\n".encode()
        )

    def test_exact_routes_query_and_origin_targets(self):
        for path, status in (
            ("http://example.com/v1/scores", 400),
            ("//example.com/v1/scores", 400),
            ("/v1//scores", 400),
            ("/v1/scores?month=x&month=y", 400),
            ("/v1/scores?unknown=1", 400),
            ("/v1/scores/alice?neighbors=-1", 400),
            ("/v1/scores/alice?neighbors=5", 400),
            ("/v1/scores/alice/more", 404),
            ("/v1/seasons/2026-10?unused=1", 400),
        ):
            with self.subTest(path=path):
                self.assertIn(
                    f" {status} ".encode(), self.get(path).split(b"\r\n", 1)[0]
                )

    def test_read_role_cannot_score_and_auth_precedes_body_read(self):
        raw = f"POST /v1/scores HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {CONFIG['read_token']}\r\nContent-Type: application/json\r\nContent-Length: 1000000\r\n\r\n".encode()
        self.assertIn(b" 401 ", self.raw(raw).split(b"\r\n", 1)[0])

    def test_duplicate_authorization_and_unused_get_body_fail(self):
        raw = f"GET /v1/scores HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {CONFIG['read_token']}\r\nAuthorization: Bearer {CONFIG['read_token']}\r\n\r\n".encode()
        self.assertIn(b" 401 ", self.raw(raw).split(b"\r\n", 1)[0])
        raw = f"GET /v1/scores HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {CONFIG['read_token']}\r\nContent-Length: 1\r\n\r\nx".encode()
        self.assertIn(b" 400 ", self.raw(raw).split(b"\r\n", 1)[0])

    def test_strict_json_and_ambiguous_framing_rejected(self):
        for headers, body in (
            ("Content-Length: 2\r\nContent-Length: 2", b"{}"),
            ("Content-Length: +2", b"{}"),
            ("Content-Length: 2\r\nTransfer-Encoding: chunked", b"{}"),
            ("Content-Length: 20", b"{}"),
            ("Content-Length: 2", b"[]"),
            ("Content-Length: 13", b'{"month":NaN}'),
            ("Content-Length: 15", b'{"a":1,"a":2}'),
        ):
            raw = (
                f"POST /admin/close HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {CONFIG['admin_token']}\r\nContent-Type: application/json\r\n{headers}\r\n\r\n".encode()
                + body
            )
            self.assertIn(b" 400 ", self.raw(raw).split(b"\r\n", 1)[0])
        for raw in ('{"a":1,"a":2}', '{"x":NaN}', '{"x":1e999}'):
            with self.assertRaises(ValueError):
                loads(raw)

    def test_admin_body_contracts_do_not_ignore_extra_fields(self):
        with running(
            server("authority", CONFIG, FakeService(), "127.0.0.1", 0)
        ) as instance:
            for path, body in (
                ("/admin/close", {"month": "2026-10", "extra": 1}),
                ("/admin/reconcile", {}),
                ("/admin/rebuild", {"extra": 1}),
                ("/admin/players/alice", {"name": "Alice", "extra": 1}),
            ):
                with self.assertRaises(HTTPError) as caught:
                    http_json(
                        f"http://127.0.0.1:{instance.server_port}" + path,
                        CONFIG["admin_token"],
                        body,
                        "PUT" if "/players/" in path else "POST",
                    )
                self.assertEqual(caught.exception.code, 400)
                caught.exception.close()

    def test_authority_feed_bypasses_operation_guard(self):
        store = FakeService()
        store.operation = lambda **options: (_ for _ in ()).throw(
            AssertionError("feed acquired public capacity")
        )
        with running(server("authority", CONFIG, store, "127.0.0.1", 0)) as instance:
            value = http_json(
                f"http://127.0.0.1:{instance.server_port}/internal/feed?shard=0&after=1",
                CONFIG["internal_token"],
            )
            self.assertEqual(value, {"shard": 0, "after": 1})
            value = http_json(
                f"http://127.0.0.1:{instance.server_port}/internal/status",
                CONFIG["internal_token"],
            )
            self.assertIn("service", value)

    def test_projection_status_query_reset_receive_shared_deadline(self):
        store = FakeService()
        with running(server("projection", CONFIG, store, "127.0.0.1", 0)) as instance:
            base = f"http://127.0.0.1:{instance.server_port}"
            http_json(base + "/internal/status", CONFIG["internal_token"])
            self.assertIsNotNone(store.last_options["cancel"])
            http_json(base + "/internal/query", CONFIG["internal_token"], {})
            self.assertLessEqual(store.last_options["deadline"] - time.monotonic(), 15)
            http_json(base + "/admin/rebuild", CONFIG["admin_token"], {})
            self.assertGreater(store.last_options["deadline"] - time.monotonic(), 50)

    def test_durable_score_receipt_uses_202_when_projection_lags(self):
        raw = f"POST /v1/scores HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {CONFIG['game_token']}\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{{}}".encode()
        response = self.raw(raw)
        self.assertIn(b" 202 ", response.split(b"\r\n", 1)[0])
        self.assertIn(b'"durable":true', response)

    def test_error_status_is_preserved(self):
        store = FakeService()
        store.query = lambda *args: (_ for _ in ()).throw(
            Error(502, "bad projection response")
        )
        with running(server("authority", CONFIG, store, "127.0.0.1", 0)) as instance:
            with self.assertRaises(HTTPError) as caught:
                http_json(
                    f"http://127.0.0.1:{instance.server_port}/v1/scores",
                    CONFIG["read_token"],
                )
            self.assertEqual(caught.exception.code, 502)
            caught.exception.close()

    def test_partial_request_retains_capacity_and_releases_on_deadline(self):
        with running(
            server(
                "authority",
                CONFIG,
                FakeService(),
                "127.0.0.1",
                0,
                concurrency=1,
                request_timeout=0.2,
            )
        ) as instance:
            with socket.create_connection(instance.server_address, timeout=2) as slow:
                slow.sendall(b"GET /health HTTP/1.1\r\n")
                end = time.monotonic() + 1
                while instance.slots.acquire(False):
                    instance.slots.release()
                    self.assertLess(time.monotonic(), end)
                    time.sleep(0.005)
                response = raw_request(
                    instance, b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n"
                )
                self.assertIn(b" 503 ", response.split(b"\r\n", 1)[0])
                self.assertEqual(slow.recv(1), b"")
                time.sleep(0.03)
                self.assertEqual(
                    http_json(
                        f"http://127.0.0.1:{instance.server_port}/health",
                        CONFIG["read_token"],
                    )["role"],
                    "authority",
                )

    def test_close_interrupts_owned_idle_request(self):
        with running(
            server("authority", CONFIG, FakeService(), "127.0.0.1", 0)
        ) as instance:
            with socket.create_connection(instance.server_address, timeout=2) as slow:
                slow.sendall(b"GET /health HTTP/1.1\r\n")
                time.sleep(0.03)
                started = time.monotonic()
                instance.shutdown()
                instance.server_close()
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(slow.recv(1), b"")


class TransportReliabilityTests(unittest.TestCase):
    def peer(self, action):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                try:
                    action(self)
                except OSError:
                    pass

        return ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    def test_proxy_is_bypassed_and_redirect_exposed(self):
        seen = []

        def reply(handler):
            seen.append(handler.path)
            handler.send_response(302)
            handler.send_header("Location", "/elsewhere")
            handler.send_header("Content-Length", "2")
            handler.end_headers()
            handler.wfile.write(b"{}")

        with (
            running(self.peer(reply)) as peer,
            patch.dict(
                os.environ,
                {
                    "http_proxy": "http://127.0.0.1:1",
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "no_proxy": "",
                    "NO_PROXY": "",
                },
            ),
        ):
            with self.assertRaises(HTTPError) as caught:
                http_json(f"http://127.0.0.1:{peer.server_port}/", CONFIG["read_token"])
            self.assertEqual(caught.exception.code, 302)
            self.assertEqual(caught.exception.read(), b"{}")
            caught.exception.close()
        self.assertEqual(seen, ["/"])

    def test_drip_cannot_extend_one_deadline(self):
        def reply(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", "100")
            handler.end_headers()
            for _ in range(20):
                handler.wfile.write(b" ")
                handler.wfile.flush()
                time.sleep(0.04)

        with running(self.peer(reply)) as peer:
            started = time.monotonic()
            with self.assertRaises(OSError):
                http_json(
                    f"http://127.0.0.1:{peer.server_port}/",
                    CONFIG["read_token"],
                    timeout=0.15,
                )
            self.assertLess(time.monotonic() - started, 0.5)

    def test_cancel_interrupts_read(self):
        entered = threading.Event()

        def reply(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", "2")
            handler.end_headers()
            entered.set()
            time.sleep(0.4)
            handler.wfile.write(b"{}")

        stop, errors = threading.Event(), []
        with running(self.peer(reply)) as peer:

            def call():
                try:
                    http_json(
                        f"http://127.0.0.1:{peer.server_port}/",
                        CONFIG["read_token"],
                        cancel=stop,
                    )
                except OSError as error:
                    errors.append(error)

            thread = threading.Thread(target=call)
            thread.start()
            self.assertTrue(entered.wait(1))
            stop.set()
            thread.join(0.3)
            self.assertFalse(thread.is_alive())
        self.assertTrue(errors)

    def test_peer_framing_lengths_and_json_are_checked(self):
        for headers, body in (
            ([("Content-Length", "abc")], b"{}"),
            ([("Content-Length", "2"), ("Content-Length", "2")], b"{}"),
            ([("Content-Length", "5")], b"{}"),
            ([("Content-Length", "13")], b'{"a":1,"a":2}'),
        ):

            def reply(handler):
                handler.send_response(200)
                for key, value in headers:
                    handler.send_header(key, value)
                handler.end_headers()
                handler.wfile.write(body)
                handler.close_connection = True

            with running(self.peer(reply)) as peer:
                with self.assertRaises(OSError):
                    http_json(
                        f"http://127.0.0.1:{peer.server_port}/", CONFIG["read_token"]
                    )
