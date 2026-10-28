"""Actual localhost HTTP, controlled peers and presentation invariants."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
import os
from pathlib import Path
import socket
import socketserver
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ad_click.common import http_json
from ad_click.http import server
from ad_click.transport import endpoint, loads, seconds

TOKEN = "private-metrics-token"
CONFIG = {role + "_token": TOKEN for role in ("read", "admin", "write", "internal")}


class Ingress:
    def __init__(self):
        self.calls = []

    def status(self):
        return {"ready": True}

    def append(self, events):
        self.calls.append(events)
        return {"accepted": len(events)}

    def read(self, **kwargs):
        return kwargs


@contextmanager
def running(service=None, **kwargs):
    httpd = server("ingress", CONFIG, service or Ingress(), "127.0.0.1", 0, **kwargs)
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
    with socket.create_connection(
        ("127.0.0.1", int(url.rsplit(":", 1)[1])), timeout=2
    ) as sock:
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


def post(body=b'{"events":[]}', headers=None, path="/events"):
    values = [
        ("Host", "local"),
        ("Authorization", "Bearer " + TOKEN),
        ("Content-Length", str(len(body))),
        ("Idempotency-Key", "test"),
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
            head, body = bytes(data).split(b"\r\n\r\n", 1)
            size = next(
                (
                    int(line.split(b":", 1)[1])
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ),
                0,
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
    def test_real_round_trip_ignores_environment_proxy(self):
        ingress = Ingress()
        with (
            running(ingress) as (_, url),
            patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "http_proxy": "http://127.0.0.1:1",
                    "NO_PROXY": "",
                    "no_proxy": "",
                },
            ),
        ):
            self.assertEqual(
                http_json(url + "/events", TOKEN, {"events": [1]}), {"accepted": 1}
            )
        self.assertEqual(ingress.calls, [[1]])

    def test_auth_precedes_body(self):
        ingress = Ingress()
        with (
            running(ingress) as (_, url),
            socket.create_connection(
                ("127.0.0.1", int(url.rsplit(":", 1)[1])), timeout=1
            ) as sock,
        ):
            sock.sendall(
                b"POST /events HTTP/1.1\r\nHost: local\r\nContent-Length: 100000\r\n\r\n"
            )
            self.assertIn(b" 401 ", sock.recv(4096))
        self.assertEqual(ingress.calls, [])

    def test_strict_json_and_framing(self):
        ingress = Ingress()
        with running(ingress) as (_, url):
            for body in (
                b'{"events":[],"events":[]}',
                b'{"events":[{"x":1,"x":2}]}',
                b'{"events":[NaN]}',
                b'{"events":[1e400]}',
                b"[]",
                b'{"events":[],"extra":1}',
            ):
                with self.subTest(body=body):
                    self.assertEqual(raw(url, post(body))[0], 400)
            for headers in (
                [("Content-Length", "13")],
                [("Transfer-Encoding", "chunked")],
                [("Transfer-Encoding", "")],
                [("Authorization", "Bearer " + TOKEN)],
            ):
                with self.subTest(headers=headers):
                    self.assertEqual(raw(url, post(headers=headers))[0], 400)
        self.assertEqual(ingress.calls, [])

    def test_truncated_type_expect_and_body_budget(self):
        with running() as (_, url):
            self.assertEqual(
                raw(url, post().replace(b"Content-Length: 13", b"Content-Length: 100"))[
                    0
                ],
                400,
            )
            self.assertEqual(
                raw(url, post(headers=[("Content-Type", "text/plain")]))[0], 415
            )
            status, payload = raw(url, post(headers=[("Expect", "100-continue")]))
            self.assertEqual(status, 417)
            self.assertNotIn(b"100 Continue", payload)
            self.assertEqual(
                raw(
                    url,
                    post().replace(b"Content-Length: 13", b"Content-Length: 1048577"),
                )[0],
                413,
            )

    def test_strict_query_and_get_body(self):
        with running() as (_, url):
            for target in (
                "/health?x=1",
                "/internal/log?partition=0&partition=1",
                "/health?bad=%ZZ",
                "http://example.com/health",
                "/internal/status?extra=1",
            ):
                self.assertEqual(
                    raw(
                        url,
                        f"GET {target} HTTP/1.1\r\nHost: local\r\nAuthorization: Bearer {TOKEN}\r\n\r\n".encode(),
                    )[0],
                    400,
                )
            self.assertEqual(
                raw(
                    url,
                    b"GET /health HTTP/1.1\r\nHost: local\r\nContent-Length: 1\r\n\r\nx",
                )[0],
                400,
            )
            self.assertEqual(
                http_json(url + "/internal/log?partition=2&after=10&limit=3", TOKEN),
                {"partition": 2, "after": 10, "limit": 3},
            )

    def test_service_failure_and_response_serialization_are_sanitized(self):
        class Broken(Ingress):
            def status(self):
                raise RuntimeError("private data should not be logged")

        with (
            running(Broken()) as (_, url),
            self.assertLogs("ad_click.http", level="WARNING") as logs,
        ):
            with self.assertRaises(HTTPError) as caught:
                http_json(url + "/internal/status", TOKEN)
            self.assertEqual(caught.exception.code, 500)
        self.assertNotIn("private data", "\n".join(logs.output))

        class Invalid(Ingress):
            def status(self):
                return {"bad": float("nan")}

        with (
            running(Invalid()) as (_, url),
            self.assertLogs("ad_click.http", level="WARNING"),
        ):
            with self.assertRaises(HTTPError) as caught:
                http_json(url + "/internal/status", TOKEN)
            self.assertEqual(caught.exception.code, 500)

    def test_capacity_is_retained_until_timed_out_business_work_exits(self):
        entered, release = threading.Event(), threading.Event()

        class Slow(Ingress):
            def append(self, events):
                entered.set()
                release.wait(3)
                return {}

        with (
            running(Slow(), concurrency=1, request_timeout=0.15) as (_, url),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            accepted = executor.submit(
                http_json, url + "/events", TOKEN, {"events": []}, timeout=2
            )
            try:
                self.assertTrue(entered.wait(1))
                for delay in (0, 0.2):
                    time.sleep(delay)
                    with self.assertRaises(HTTPError) as caught:
                        http_json(url + "/events", TOKEN, {"events": ["x" * 65536]})
                    self.assertEqual(caught.exception.code, 503)
            finally:
                release.set()
            with self.assertRaises(OSError):
                accepted.result(2)

    def test_shutdown_drains_accepted_work(self):
        entered, release = threading.Event(), threading.Event()

        class Slow(Ingress):
            def append(self, events):
                entered.set()
                release.wait(3)
                return {}

        with (
            running(Slow()) as (httpd, url),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            accepted = executor.submit(
                http_json, url + "/events", TOKEN, {"events": []}
            )
            self.assertTrue(entered.wait(1))
            httpd.shutdown()
            closed = executor.submit(httpd.server_close)
            try:
                time.sleep(0.05)
                self.assertFalse(closed.done())
            finally:
                release.set()
            closed.result(2)
            with suppress(OSError):
                accepted.result(2)

    def test_slow_headers_and_body_have_absolute_deadline(self):
        for payload in (
            b"GET /health HTTP/1.1\r\nHost: ",
            post().split(b"\r\n\r\n")[0] + b"\r\n\r\n{",
        ):
            with (
                self.subTest(payload=payload),
                running(request_timeout=0.12) as (_, url),
                socket.create_connection(
                    ("127.0.0.1", int(url.rsplit(":", 1)[1])), timeout=1
                ) as sock,
            ):
                sock.sendall(payload)
                started = time.monotonic()
                self.assertEqual(sock.recv(4096), b"")
                self.assertLess(time.monotonic() - started, 0.7)

    def test_response_framing_length_and_json(self):
        for response in (
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}",
            b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{}",
            b'HTTP/1.1 200 OK\r\nContent-Length: 13\r\n\r\n{"x":1,"x":2}',
        ):
            with (
                self.subTest(response=response),
                peer(lambda sock: sock.sendall(response)) as url,
            ):
                with self.assertRaises((ValueError, OSError)):
                    http_json(url, TOKEN)
        with peer(
            lambda sock: sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
        ) as url:
            with self.assertRaises(ValueError):
                http_json(url, TOKEN, max_bytes=20)

    def test_chunked_response_and_refusal_to_redirect(self):
        with peer(
            lambda sock: sock.sendall(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n2\r\n{}\r\n0\r\n\r\n"
            )
        ) as url:
            self.assertEqual(http_json(url, TOKEN), {})
        with peer(
            lambda sock: sock.sendall(
                b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/private\r\nContent-Length: 0\r\n\r\n"
            )
        ) as url:
            with self.assertRaises(HTTPError) as caught:
                http_json(url, TOKEN)
            self.assertEqual(caught.exception.code, 302)

    def test_response_deadline_and_cancel(self):
        def drip(sock):
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
            for _ in range(20):
                sock.sendall(b" ")
                time.sleep(0.03)

        with peer(drip) as url:
            with self.assertRaises((TimeoutError, OSError)):
                http_json(url, TOKEN, timeout=0.12)
        cancel = threading.Event()
        with peer(drip) as url, ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(http_json, url, TOKEN, cancel=cancel)
            time.sleep(0.05)
            cancel.set()
            with self.assertRaises((TimeoutError, OSError)):
                future.result(1)

    def test_api_owns_one_query_service_and_closes_it(self):
        queries = Mock()
        queries.aggregate.return_value = {"count": 7}
        with patch(
            "ad_click.service.QueryService", return_value=queries
        ) as constructor:
            httpd = server("api", CONFIG, None, "127.0.0.1", 0)
            thread = threading.Thread(
                target=httpd.serve_forever, kwargs={"poll_interval": 0.01}
            )
            thread.start()
            try:
                url = "http://127.0.0.1:" + str(httpd.server_port)
                self.assertEqual(
                    http_json(url + "/query", TOKEN, {"window": 2}), {"count": 7}
                )
                self.assertEqual(
                    http_json(
                        url + "/v1/ads/ad%20one/aggregated_count?window=3&country=US",
                        TOKEN,
                    ),
                    {"count": 7},
                )
                self.assertEqual(
                    queries.aggregate.call_args.args[0],
                    {"window": 3, "filters": {"country": "US"}, "ad_id": "ad one"},
                )
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join()
            constructor.assert_called_once()
            queries.close.assert_called_once()

    def test_transport_configuration_validation(self):
        for duration in (0, True, float("inf"), 10**10000):
            with self.assertRaises(ValueError):
                seconds(duration)
        for url in (
            "ftp://local",
            "http://user:pass@local",
            "http://local/#secret",
            "http://local:0",
        ):
            with self.assertRaises(ValueError):
                endpoint(url)
        with self.assertRaises(ValueError):
            loads('{"a":1,"a":2}')
        with self.assertRaises(ValueError):
            server("ingress", CONFIG, Ingress(), "127.0.0.1", 0, concurrency=0)


if __name__ == "__main__":
    unittest.main()
