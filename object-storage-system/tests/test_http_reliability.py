"""Network contract, deadline and bounded-capacity regressions."""

from contextlib import contextmanager
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from unittest.mock import patch
from urllib.error import HTTPError
import socket
import sys
import threading
import time
from types import SimpleNamespace
import unittest
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from object_storage.common import Error, request as http_json
from object_storage.http import server
from object_storage.service import Catalog, Service
from object_storage.common import sha

TOKEN = "a" * 48
CONFIG = {
    "internal_token": "b" * 48,
    "admin_token": "c" * 48,
    "users": {"alice": TOKEN},
    "metadata_urls": [],
}


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
        self.last_deadline = None
        self.catalog = SimpleNamespace(
            listing=lambda user: {"buckets": [{"name": "bucket-test", "user": user}]},
            bucket=lambda name, user: {"id": "bucket", "name": name, "user": user},
            start=lambda *args: {"upload": "id"},
        )

    @contextmanager
    def operation(self, *, deadline, cancel):
        self.last_deadline = deadline
        self.in_operation = True
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

    def listing(self, *args, **kwargs):
        assert self.in_operation and self.in_guard
        return {
            "objects": [],
            "common_prefixes": ["CA/"],
            "delimiter": kwargs["delimiter"],
        }

    def complete(self, *args):
        return {"complete": True}

    def maintenance(self, grace):
        assert self.in_operation and self.in_guard
        return {"grace": grace, "remaining": self.last_deadline - time.monotonic()}


class HTTPReliabilityTests(unittest.TestCase):
    def test_bucket_delete_blocks_unknown_publication_until_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            config = {
                **CONFIG,
                "metadata_urls": ["http://metadata"],
                "data_urls": ["http://a", "http://b", "http://c"],
                "continuation_secret": "secret",
            }
            store = Service(config, Catalog(Path(root) / "catalog.sqlite"))
            self.addCleanup(store.close)
            bucket = store.catalog.create("bucket-test", "alice")
            store.catalog.prepare_publication(
                bucket["id"], "key", "request", [], 0, sha(b""), False
            )
            with (
                patch("object_storage.http.request", return_value={"rows": []}),
                running(server("api", config, store, "127.0.0.1", 0)) as instance,
            ):
                with self.assertRaises(HTTPError) as caught:
                    http_json(
                        f"http://127.0.0.1:{instance.server_port}/v1/buckets/bucket-test",
                        TOKEN,
                        method="DELETE",
                    )
                self.assertEqual(caught.exception.code, 409)
                caught.exception.close()
                self.assertEqual(
                    store.catalog.bucket("bucket-test", "alice")["id"], bucket["id"]
                )

    def request(self, raw, store=None):
        instance = server("api", CONFIG, store or FakeService(), "127.0.0.1", 0)
        with running(instance):
            return raw_request(instance, raw)

    def raw(self, path, method="GET", extra="", body=b""):
        return (
            f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {TOKEN}\r\n"
            + extra
            + "\r\n"
        ).encode() + body

    def test_owned_buckets_and_delimiter_are_dispatched_under_guard(self):
        service = FakeService()
        with running(server("api", CONFIG, service, "127.0.0.1", 0)) as peer:
            base = f"http://127.0.0.1:{peer.server_port}"
            self.assertEqual(
                http_json(base + "/v1/buckets", TOKEN)["buckets"][0]["user"], "alice"
            )
            self.assertEqual(
                http_json(
                    base + "/v1/buckets/bucket-test/objects?delimiter=%2F", TOKEN
                )["common_prefixes"],
                ["CA/"],
            )
        self.assertFalse(service.in_guard)
        self.assertFalse(service.in_operation)

    def test_duplicate_authorization_is_rejected(self):
        response = self.request(
            self.raw("/v1/buckets", extra=f"Authorization: Bearer {TOKEN}\r\n")
        )
        self.assertIn(b" 401 ", response.split(b"\r\n", 1)[0])

    def test_duplicate_object_contract_headers_are_rejected(self):
        for name in ("Host", "X-SHA256", "Idempotency-Key"):
            extra = f"{name}: one\r\n" + ("" if name == "Host" else f"{name}: two\r\n")
            response = self.request(self.raw("/v1/buckets", extra=extra))
            self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])

    def test_strict_json_duplicate_nonfinite_and_shape(self):
        for body in (b"[]", b'{"key":1,"key":2}', b'{"x":NaN}', b'{"x":1e999}'):
            with self.subTest(body=body):
                raw = self.raw(
                    "/v1/buckets/bucket-test/uploads",
                    "POST",
                    f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n",
                    body,
                )
                self.assertIn(b" 400 ", self.request(raw).split(b"\r\n", 1)[0])

    def test_ambiguous_and_truncated_framing(self):
        variants = (
            "Content-Length: 2\r\nContent-Length: 2\r\n",
            "Content-Length: +2\r\n",
            "Content-Length: 2\r\nTransfer-Encoding: chunked\r\n",
            "Content-Length: 20\r\n",
        )
        for headers in variants:
            with self.subTest(headers=headers):
                raw = self.raw(
                    "/v1/buckets/bucket-test/uploads",
                    "POST",
                    "Content-Type: application/json\r\n" + headers,
                    b"{}",
                )
                self.assertIn(b" 400 ", self.request(raw).split(b"\r\n", 1)[0])

    def test_body_media_and_unused_bodies_are_rejected(self):
        for raw in (
            self.raw("/v1/buckets", "GET", "Content-Length: 2\r\n", b"{}"),
            self.raw("/v1/buckets/bucket-test", "PUT", "Content-Length: 2\r\n", b"{}"),
            self.raw(
                "/v1/buckets/bucket-test/uploads",
                "POST",
                "Content-Type: text/plain\r\nContent-Length: 2\r\n",
                b"{}",
            ),
        ):
            self.assertIn(b" 400 ", self.request(raw).split(b"\r\n", 1)[0])

    def test_origin_and_exact_query_contracts(self):
        for path in (
            "http://example.com/v1/buckets",
            "//example.com/v1/buckets",
            "/v1//buckets",
            "/v1/buckets/%61",
            "/v1/buckets?ignored=x",
            "/v1/buckets/bucket-test/objects?limit=-1",
            "/v1/buckets/bucket-test/objects?prefix=a&prefix=b",
        ):
            with self.subTest(path=path):
                self.assertIn(
                    b" 400 ", self.request(self.raw(path)).split(b"\r\n", 1)[0]
                )

    def test_complete_requires_empty_json_object(self):
        self.assertIn(
            b" 400 ",
            self.request(
                self.raw(
                    "/v1/uploads/id/complete",
                    "POST",
                    "Content-Type: application/json\r\nContent-Length: 7\r\n",
                    b'{"x":1}',
                )
            ).split(b"\r\n", 1)[0],
        )

    def test_slow_request_capacity_is_retained_then_released(self):
        instance = server(
            "api",
            CONFIG,
            FakeService(),
            "127.0.0.1",
            0,
            concurrency=1,
            request_timeout=0.2,
        )
        with running(instance):
            with socket.create_connection(instance.server_address, timeout=2) as slow:
                slow.sendall(b"GET /health HTTP/1.1\r\n")
                end = time.monotonic() + 1
                while instance.slots.acquire(False):
                    instance.slots.release()
                    if time.monotonic() > end:
                        self.fail("request did not acquire capacity")
                    time.sleep(0.005)
                response = raw_request(
                    instance, b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n"
                )
                self.assertIn(b" 503 ", response.split(b"\r\n", 1)[0])
                self.assertEqual(slow.recv(1), b"")
                end = time.monotonic() + 1
                while not instance.slots.acquire(False):
                    if time.monotonic() > end:
                        self.fail("expired request did not release capacity")
                    time.sleep(0.005)
                instance.slots.release()
                self.assertEqual(
                    http_json(f"http://127.0.0.1:{instance.server_port}/health", TOKEN)[
                        "role"
                    ],
                    "api",
                )

    def test_close_interrupts_accepted_idle_socket(self):
        instance = server("api", CONFIG, FakeService(), "127.0.0.1", 0)
        with running(instance):
            with socket.create_connection(instance.server_address, timeout=2) as slow:
                slow.sendall(b"GET /health HTTP/1.1\r\n")
                time.sleep(0.03)
                started = time.monotonic()
                instance.shutdown()
                instance.server_close()
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(slow.recv(1), b"")

    def test_maintenance_receives_longer_shared_budget(self):
        service = FakeService()
        with running(server("api", CONFIG, service, "127.0.0.1", 0)) as peer:
            value = http_json(
                f"http://127.0.0.1:{peer.server_port}/admin/maintenance",
                CONFIG["admin_token"],
                {"grace": 60},
            )
        self.assertGreater(value["remaining"], 50)

    def test_invalid_upstream_descriptor_preserves_502(self):
        service = FakeService()

        def bad_descriptor(*_args, **_kwargs):
            raise Error(502, "invalid upstream descriptor")

        service.listing = bad_descriptor
        response = self.request(
            self.raw("/v1/buckets/bucket-test/objects"), store=service
        )
        self.assertIn(b" 502 ", response.split(b"\r\n", 1)[0])


class TransportReliabilityTests(unittest.TestCase):
    def test_raw_bytes_are_preserved_in_both_directions(self):
        data = bytes(range(256)) * 4096

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_PUT(self):
                size = int(self.headers["Content-Length"])
                body = self.rfile.read(size)
                if self.headers["Content-Type"] != "application/octet-stream":
                    self.send_error(400)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with running(ThreadingHTTPServer(("127.0.0.1", 0), Handler)) as peer:
            result = http_json(
                f"http://127.0.0.1:{peer.server_port}/raw", TOKEN, data, "PUT", raw=True
            )
        self.assertEqual(result, data)

    def peer(self, action):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                try:
                    action(self)
                except OSError:
                    pass

        return ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    def test_environment_proxy_is_not_used(self):
        def action(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", "2")
            handler.end_headers()
            handler.wfile.write(b"{}")

        with running(self.peer(action)) as peer:
            with patch.dict(
                os.environ,
                {
                    "http_proxy": "http://127.0.0.1:1",
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "no_proxy": "",
                    "NO_PROXY": "",
                },
            ):
                self.assertEqual(
                    http_json(f"http://127.0.0.1:{peer.server_port}/", TOKEN), {}
                )

    def test_redirect_is_exposed_without_forwarding_credentials(self):
        seen = []

        def action(handler):
            seen.append(handler.path)
            handler.send_response(302)
            handler.send_header("Location", "/target")
            handler.send_header("Content-Length", "2")
            handler.end_headers()
            handler.wfile.write(b"{}")

        with running(self.peer(action)) as peer:
            with self.assertRaises(HTTPError) as raised:
                http_json(f"http://127.0.0.1:{peer.server_port}/", TOKEN)
            self.assertEqual(raised.exception.code, 302)
            self.assertEqual(raised.exception.read(), b"{}")
            raised.exception.close()
            self.assertEqual(seen, ["/"])

    def test_dripping_response_cannot_extend_total_deadline(self):
        def action(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", "100")
            handler.end_headers()
            for _ in range(30):
                handler.wfile.write(b" ")
                handler.wfile.flush()
                time.sleep(0.04)

        with running(self.peer(action)) as peer:
            started = time.monotonic()
            with self.assertRaises((TimeoutError, OSError)):
                http_json(f"http://127.0.0.1:{peer.server_port}/", TOKEN, timeout=0.2)
            self.assertLess(time.monotonic() - started, 0.7)

    def test_cancel_event_interrupts_inflight_response(self):
        entered = threading.Event()

        def action(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", "2")
            handler.end_headers()
            entered.set()
            time.sleep(0.4)
            handler.wfile.write(b"{}")

        cancel, errors = threading.Event(), []
        with running(self.peer(action)) as peer:

            def call():
                try:
                    http_json(
                        f"http://127.0.0.1:{peer.server_port}/",
                        TOKEN,
                        timeout=3,
                        cancel=cancel,
                    )
                except (OSError, TimeoutError) as exc:
                    errors.append(exc)

            thread = threading.Thread(target=call)
            thread.start()
            self.assertTrue(entered.wait(1))
            cancel.set()
            thread.join(0.3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(errors)

    def test_response_framing_and_json_are_checked(self):
        variants = [
            ([("Content-Length", "abc")], b"{}"),
            ([("Content-Length", "2"), ("Content-Length", "2")], b"{}"),
            ([("Content-Length", "10")], b"{}"),
            ([("Content-Length", "13")], b'{"x":1,"x":2}'),
            ([("Content-Type", "text/plain"), ("Content-Length", "2")], b"{}"),
        ]
        for headers, body in variants:

            def action(handler):
                handler.send_response(200)
                for key, value in headers:
                    handler.send_header(key, value)
                handler.end_headers()
                handler.wfile.write(body)

            with self.subTest(headers=headers), running(self.peer(action)) as peer:
                with self.assertRaises(OSError):
                    http_json(f"http://127.0.0.1:{peer.server_port}/", TOKEN)
