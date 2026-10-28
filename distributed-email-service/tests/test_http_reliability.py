"""Exercise framing, direct RPC deadlines and bounded request ownership."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from distributed_email.common import http_json
from distributed_email.http import server
from distributed_email.transport import loads

TOKEN = "a" * 48
CONFIG = {"internal_token": "b" * 48, "users": {"alice": TOKEN}}


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


class FakeStore:
    def initialize(self, _sql):
        pass

    def owner(self, _user):
        pass

    def status(self):
        return {"lineage": "lineage", "position": 0}

    def listing(self, _user, **_params):
        return {"messages": []}

    def update(self, *_args, **_kwargs):
        return {"updated": True}


class HTTPReliabilityTests(unittest.TestCase):
    def request(self, raw, *, role="primary", store=None):
        instance = server(role, CONFIG, store or FakeStore(), "127.0.0.1", 0)
        with running(instance):
            return raw_request(instance, raw)

    def test_strict_json_rejects_duplicate_nonfinite_and_overflow(self):
        for raw in ('{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                loads(raw)

    def test_json_framing_rejections(self):
        variants = [
            b"Content-Length: abc\r\n",
            b"Content-Length: 2\r\nContent-Length: 2\r\n",
            b"Content-Length: +2\r\n",
            b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n",
            b"Content-Length: 20\r\n",
        ]
        for headers in variants:
            with self.subTest(headers=headers):
                response = self.request(
                    b"PUT /v1/messages/id HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer "
                    + TOKEN.encode()
                    + b"\r\nContent-Type: application/json\r\n"
                    + headers
                    + b"\r\n{}"
                )
                self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])

    def test_json_shape_and_duplicate_fields_are_not_accepted(self):
        for body in (
            b"[]",
            b'{"version":1,"read":true,"read":false}',
            b'{"version":NaN,"read":true}',
        ):
            response = self.request(
                b"PUT /v1/messages/id HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer "
                + TOKEN.encode()
                + b"\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])

    def test_unknown_repeated_and_write_query_fields_fail(self):
        for path in (
            "/v1/messages?typo=x",
            "/v1/messages?q=a&q=b",
            "/v1/messages/id?read=true",
            "/v1/messages?before=-1",
        ):
            response = self.request(
                f"GET {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {TOKEN}\r\n\r\n".encode()
            )
            self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])

    def test_duplicate_authorization_is_rejected(self):
        raw = f"GET /v1/messages HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {TOKEN}\r\nAuthorization: Bearer {TOKEN}\r\n\r\n".encode()
        self.assertIn(b" 401 ", self.request(raw).split(b"\r\n", 1)[0])

    def test_origin_route_and_attachment_path_are_exact(self):
        for path in (
            "http://example.com/v1/messages",
            "//example.com/v1/messages",
            "/v1/messages/%61",
            "/v1//messages",
        ):
            response = self.request(
                f"GET {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {TOKEN}\r\n\r\n".encode()
            )
            self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])

    def test_slow_socket_occupies_one_slot_and_drains_on_close(self):
        instance = server(
            "primary",
            CONFIG,
            FakeStore(),
            "127.0.0.1",
            0,
            concurrency=1,
            request_timeout=0.3,
        )
        with running(instance):
            slow = socket.create_connection(instance.server_address, timeout=2)
            try:
                slow.sendall(b"GET /health HTTP/1.1\r\n")
                end = time.monotonic() + 1
                while instance.slots.acquire(False):
                    instance.slots.release()
                    if time.monotonic() > end:
                        self.fail("first request did not retain capacity")
                    time.sleep(0.005)
                response = raw_request(
                    instance, b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n"
                )
                self.assertIn(b" 503 ", response.split(b"\r\n", 1)[0])
                self.assertEqual(slow.recv(1), b"")
                time.sleep(0.03)
                value = http_json(
                    f"http://127.0.0.1:{instance.server_port}/health", TOKEN
                )
                self.assertEqual(value["role"], "primary")
            finally:
                slow.close()

    def test_idle_socket_is_interrupted_by_server_close(self):
        instance = server(
            "primary", CONFIG, FakeStore(), "127.0.0.1", 0, request_timeout=5
        )
        with running(instance):
            with socket.create_connection(instance.server_address, timeout=2) as sock:
                sock.sendall(b"GET /health HTTP/1.1\r\n")
                time.sleep(0.03)
                started = time.monotonic()
                instance.shutdown()
                instance.server_close()
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(sock.recv(1), b"")


class TransportReliabilityTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
