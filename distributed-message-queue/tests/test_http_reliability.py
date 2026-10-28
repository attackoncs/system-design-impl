"""Actual localhost HTTP plus controlled slow-peer and application faults."""

from contextlib import contextmanager, suppress
from concurrent.futures import ThreadPoolExecutor
import http.client
import os
from pathlib import Path
import socket
import socketserver
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from message_queue.common import rpc
from message_queue.http import server
from message_queue.lifecycle import Maintenance
from message_queue.transport import endpoint, loads, seconds

TOKEN = "private-test-token"


class Echo:
    def __init__(self):
        self.calls = []

    def dispatch(self, path, body):
        self.calls.append((path, body))
        return {"path": path, "body": body}


@contextmanager
def running(service=None, **kwargs):
    httpd = server("127.0.0.1", 0, service or Echo(), TOKEN, **kwargs)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield httpd, "http://127.0.0.1:" + str(httpd.server_port)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def raw(url, request):
    port = int(url.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
        sock.sendall(request)
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            part = sock.recv(65536)
            if not part:
                break
            chunks.append(part)
    response = b"".join(chunks)
    return int(response.split(b" ", 2)[1]), response


def post(body=b"{}", headers=None, path="/health"):
    values = [
        ("Host", "localhost"),
        ("Authorization", "Bearer " + TOKEN),
        ("Content-Length", str(len(body))),
    ]
    values.extend(headers or [])
    return (
        f"POST {path} HTTP/1.1\r\n".encode()
        + b"".join((key + ": " + value + "\r\n").encode() for key, value in values)
        + b"\r\n"
        + body
    )


@contextmanager
def peer(action):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            data = bytearray()
            while b"\r\n\r\n" not in data:
                part = self.request.recv(65536)
                if not part:
                    return
                data.extend(part)
            headers, body = bytes(data).split(b"\r\n\r\n", 1)
            size = int(
                next(
                    line.split(b":", 1)[1]
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
            )
            while len(body) < size:
                part = self.request.recv(65536)
                if not part:
                    return
                body += part
            with suppress(OSError):
                action(self.request)

    httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(httpd.server_address[1])
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


class HTTPReliabilityTests(unittest.TestCase):
    def test_rpc_round_trip_and_no_environment_proxy(self):
        with running() as (_httpd, url):
            with patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "http_proxy": "http://127.0.0.1:1",
                    "NO_PROXY": "",
                    "no_proxy": "",
                },
            ):
                self.assertEqual(
                    rpc(url, "/health", {"n": 3}, TOKEN),
                    {"path": "/health", "body": {"n": 3}},
                )

    def test_unauthorized_is_rejected_before_waiting_for_body(self):
        service = Echo()
        with running(service) as (_httpd, url):
            with socket.create_connection(
                ("127.0.0.1", int(url.rsplit(":", 1)[1])), timeout=1
            ) as sock:
                sock.sendall(
                    b"POST /health HTTP/1.1\r\nHost: local\r\nContent-Length: 100000\r\n\r\n"
                )
                self.assertIn(b" 401 ", sock.recv(4096))
            self.assertEqual(service.calls, [])

    def test_duplicate_and_nonfinite_json_rejected_without_dispatch(self):
        service = Echo()
        with running(service) as (_httpd, url):
            for body in (
                b'{"x":1,"x":2}',
                b'{"nested":{"x":1,"x":2}}',
                b'{"x":NaN}',
                b'{"x":Infinity}',
                b'{"x":1e400}',
                b"[]",
            ):
                with self.subTest(body=body):
                    self.assertEqual(raw(url, post(body))[0], 400)
            self.assertEqual(service.calls, [])

    def test_ambiguous_framing_is_rejected(self):
        service = Echo()
        with running(service) as (_httpd, url):
            for headers in (
                [("Content-Length", "2")],
                [("Content-Length", "3")],
                [("Transfer-Encoding", "chunked")],
                [("Transfer-Encoding", "")],
                [("Authorization", "Bearer " + TOKEN)],
            ):
                with self.subTest(headers=headers):
                    self.assertEqual(raw(url, post(headers=headers))[0], 400)
            self.assertEqual(service.calls, [])

    def test_truncated_wrong_type_and_unknown_target_rejected(self):
        service = Echo()
        with running(service) as (_httpd, url):
            short = post(b'{"x":1}').replace(
                b"Content-Length: 7", b"Content-Length: 100"
            )
            self.assertEqual(raw(url, short)[0], 400)
            self.assertEqual(
                raw(url, post(headers=[("Content-Type", "text/plain")]))[0], 415
            )
            self.assertEqual(raw(url, post(path="/health?extra=1"))[0], 400)
            self.assertEqual(service.calls, [])

    def test_expect_rejected_after_auth_without_interim_continue(self):
        with running() as (_httpd, url):
            status, response = raw(url, post(headers=[("Expect", "100-continue")]))
            self.assertEqual(status, 417)
            self.assertNotIn(b"100 Continue", response)

    def test_request_size_limit_rejects_without_body(self):
        with running() as (_httpd, url):
            request = post().replace(b"Content-Length: 2", b"Content-Length: 33554433")
            self.assertEqual(raw(url, request)[0], 413)

    def test_handler_errors_are_sanitized_and_http_error_compatible(self):
        class Broken:
            def dispatch(self, _path, _body):
                raise RuntimeError("private details must not be returned")

        with running(Broken()) as (_httpd, url):
            with self.assertLogs("message_queue.http", level="WARNING") as logs:
                with self.assertRaises(HTTPError) as caught:
                    rpc(url, "/health", {}, TOKEN)
            self.assertEqual(caught.exception.code, 500)
            self.assertNotIn("private details", "\n".join(logs.output))

    def test_invalid_service_result_gets_sanitized_500(self):
        class Broken:
            def dispatch(self, _path, _body):
                return {"private": float("nan")}

        with running(Broken()) as (_httpd, url):
            with self.assertLogs("message_queue.http", level="WARNING"):
                status, response = raw(url, post())
            self.assertEqual(status, 500)
            self.assertNotIn(b'"private"', response)

    def test_capacity_rejects_and_preserves_slot_until_work_exits(self):
        entered, release = threading.Event(), threading.Event()

        class Slow:
            def dispatch(self, _path, _body):
                entered.set()
                release.wait(2)
                return {"done": True}

        with running(Slow(), concurrency=1, request_timeout=0.15) as (_httpd, url):
            with ThreadPoolExecutor(max_workers=1) as executor:
                accepted = executor.submit(rpc, url, "/health", {}, TOKEN, timeout=2)
                try:
                    self.assertTrue(entered.wait(1))
                    with self.assertRaises(HTTPError) as caught:
                        # Larger than the old single 8192-byte rejection drain:
                        # unread request bytes must not reset the 503 response.
                        rpc(url, "/health", {"payload": "x" * 65536}, TOKEN)
                    self.assertEqual(caught.exception.code, 503)
                    time.sleep(0.2)
                    with self.assertRaises(HTTPError) as caught:
                        rpc(url, "/health", {}, TOKEN)
                    self.assertEqual(caught.exception.code, 503)
                finally:
                    release.set()
                with self.assertRaises((OSError, http.client.HTTPException)):
                    accepted.result(timeout=2)

    def test_server_close_waits_for_accepted_application_work(self):
        entered, release = threading.Event(), threading.Event()

        class Slow:
            def dispatch(self, _path, _body):
                entered.set()
                release.wait(2)
                return {"done": True}

        with running(Slow()) as (httpd, url):
            with ThreadPoolExecutor(max_workers=2) as executor:
                accepted = executor.submit(rpc, url, "/health", {}, TOKEN, timeout=2)
                self.assertTrue(entered.wait(1))
                httpd.shutdown()
                closed = executor.submit(httpd.server_close)
                try:
                    time.sleep(0.1)
                    self.assertFalse(closed.done())
                finally:
                    release.set()
                closed.result(timeout=2)
                with self.assertRaises((OSError, http.client.HTTPException)):
                    accepted.result(timeout=2)

    def test_slow_headers_have_one_absolute_deadline(self):
        with running(request_timeout=0.15) as (_httpd, url):
            start = time.monotonic()
            with socket.create_connection(
                ("127.0.0.1", int(url.rsplit(":", 1)[1])), timeout=1
            ) as sock:
                sock.sendall(b"POST /health HTTP/1.1\r\nHost: local\r\n")
                self.assertEqual(sock.recv(4096), b"")
            self.assertLess(time.monotonic() - start, 0.8)

    def test_slow_body_has_one_deadline_and_never_dispatches(self):
        service = Echo()
        with running(service, request_timeout=0.15) as (_httpd, url):
            with socket.create_connection(
                ("127.0.0.1", int(url.rsplit(":", 1)[1])), timeout=1
            ) as sock:
                sock.sendall(post(b"{}").replace(b"\r\n\r\n{}", b"\r\n\r\n{"))
                started = time.monotonic()
                self.assertEqual(sock.recv(4096), b"")
                self.assertLess(time.monotonic() - started, 0.8)
            self.assertEqual(service.calls, [])

    def test_maintenance_failure_is_visible_to_authenticated_health(self):
        worker = Maintenance(lambda: None)
        worker.last_error = "queue maintenance failed; will retry"
        with running(maintenance=worker) as (_httpd, url):
            self.assertEqual(
                rpc(url, "/health", {}, TOKEN)["maintenance_error"], worker.last_error
            )


class RPCPeerTests(unittest.TestCase):
    def test_valid_chunked_response_and_protocol_failure_family(self):
        with peer(
            lambda sock: sock.sendall(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                b"2\r\n{}\r\n0\r\n\r\n"
            )
        ) as url:
            self.assertEqual(rpc(url, "/health", {}, TOKEN), {})
        with peer(lambda sock: sock.sendall(b"not HTTP\r\n\r\n")) as url:
            with self.assertRaises(OSError):
                rpc(url, "/health", {}, TOKEN)

    def test_redirect_is_not_followed(self):
        with peer(
            lambda sock: sock.sendall(
                b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/stolen\r\nContent-Length: 0\r\n\r\n"
            )
        ) as url:
            with self.assertRaises(HTTPError) as caught:
                rpc(url, "/health", {}, TOKEN)
            self.assertEqual(caught.exception.code, 302)

    def test_duplicate_length_and_transfer_conflicts_rejected(self):
        for headers in (
            b"Content-Length: 2\r\nContent-Length: 2\r\n",
            b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n",
            b"Transfer-Encoding: gzip\r\n",
        ):
            with self.subTest(headers=headers):
                with peer(
                    lambda sock: sock.sendall(
                        b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n{}"
                    )
                ) as url:
                    with self.assertRaises(ValueError):
                        rpc(url, "/health", {}, TOKEN)

    def test_truncation_and_bounded_length_rejected(self):
        for headers, body, bound in (
            (b"Content-Length: 10\r\n", b"{}", 100),
            (b"Content-Length: 50\r\n", b"{}", 20),
            (b"Connection: close\r\n", b'{"long":"0123456789"}', 8),
        ):
            with self.subTest(headers=headers, bound=bound):
                with peer(
                    lambda sock: sock.sendall(
                        b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n" + body
                    )
                ) as url:
                    with self.assertRaises(ValueError):
                        rpc(url, "/health", {}, TOKEN, max_bytes=bound)

    def test_duplicate_response_json_and_nonfinite_rejected(self):
        for body in (b'{"a":1,"a":2}', b'{"value":1e400}'):
            with self.subTest(body=body):
                with peer(
                    lambda sock: sock.sendall(
                        b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + body
                    )
                ) as url:
                    with self.assertRaises(ValueError):
                        rpc(url, "/health", {}, TOKEN)

    def test_deadline_interrupts_dripping_response_headers(self):
        def slow(sock):
            for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}":
                sock.sendall(bytes([byte]))
                time.sleep(0.03)

        with peer(slow) as url:
            started = time.monotonic()
            with self.assertRaises((OSError, ValueError, http.client.HTTPException)):
                rpc(url, "/health", {}, TOKEN, timeout=0.15)
            self.assertLess(time.monotonic() - started, 0.8)

    def test_cancellation_interrupts_body_and_joins_watcher(self):
        reading = threading.Event()

        def slow(sock):
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{")
            reading.set()
            time.sleep(0.4)

        cancel = threading.Event()
        with peer(slow) as url:
            with ThreadPoolExecutor(max_workers=1) as executor:
                call = executor.submit(
                    rpc, url, "/health", {}, TOKEN, timeout=2, cancel=cancel
                )
                self.assertTrue(reading.wait(1))
                started = time.monotonic()
                cancel.set()
                with self.assertRaises(
                    (OSError, ValueError, http.client.HTTPException)
                ):
                    call.result(timeout=1)
                self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(
            any(thread.name == "queue-io-deadline" for thread in threading.enumerate())
        )

    def test_invalid_local_configuration_rejected_before_network(self):
        for value in (True, 0, -1, float("inf"), float("nan"), 10**10000):
            with self.assertRaises(ValueError):
                seconds(value)
        for url in (
            "ftp://host",
            "http://user:pass@host",
            "http://host?x=1",
            "http://host/#fragment",
            "http://host:0",
            "http://bad\r\nhost",
        ):
            with self.assertRaises(ValueError):
                endpoint(url)
        with self.assertRaises(ValueError):
            rpc("http://127.0.0.1:1", "//outside", {}, TOKEN)
        with self.assertRaises(ValueError):
            loads('{"a":1,"a":2}')


if __name__ == "__main__":
    unittest.main()
