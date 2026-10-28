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
import tracemalloc
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from metrics_monitor.common import Error, http_json, number
from metrics_monitor.http import Exporter, chart, server
from metrics_monitor.transport import endpoint, loads, seconds

TOKEN = "private-metrics-token"
CONFIG = {role + "_token": TOKEN for role in ("read", "admin", "push", "exporter")}


class Collector:
    def __init__(self):
        self.calls = []

    def health(self):
        return {"ready": True}

    def enqueue(self, samples, request_id):
        self.calls.append((samples, request_id))
        return {"accepted": len(samples)}


@contextmanager
def running(service=None, **kwargs):
    httpd = server("127.0.0.1", 0, CONFIG, collector=service or Collector(), **kwargs)
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


def post(body=b'{"samples":[]}', headers=None, path="/push"):
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
    def test_round_trip_ignores_environment_proxy(self):
        collector = Collector()
        with (
            running(collector) as (_, url),
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
                http_json(
                    url + "/push",
                    "POST",
                    {"samples": [1]},
                    TOKEN,
                    {"Idempotency-Key": "id"},
                ),
                {"accepted": 1},
            )
        self.assertEqual(collector.calls, [([1], "id")])

    def test_auth_before_waiting_for_body(self):
        collector = Collector()
        with (
            running(collector) as (_, url),
            socket.create_connection(
                ("127.0.0.1", int(url.rsplit(":", 1)[1])), timeout=1
            ) as sock,
        ):
            sock.sendall(
                b"POST /push HTTP/1.1\r\nHost: local\r\nContent-Length: 100000\r\n\r\n"
            )
            self.assertIn(b" 401 ", sock.recv(4096))
        self.assertEqual(collector.calls, [])

    def test_duplicate_nonfinite_and_unknown_json_rejected(self):
        collector = Collector()
        with running(collector) as (_, url):
            for body in (
                b'{"samples":[],"samples":[]}',
                b'{"samples":[{"x":1,"x":2}]}',
                b'{"samples":[NaN]}',
                b'{"samples":[1e400]}',
                b"[]",
                b'{"samples":[],"extra":1}',
            ):
                with self.subTest(body=body):
                    self.assertEqual(raw(url, post(body))[0], 400)
        self.assertEqual(collector.calls, [])

    def test_duplicate_and_transfer_framing_rejected(self):
        with running() as (_, url):
            for headers in (
                [("Content-Length", "14")],
                [("Transfer-Encoding", "chunked")],
                [("Transfer-Encoding", "")],
                [("Authorization", "Bearer " + TOKEN)],
                [("Idempotency-Key", "other")],
            ):
                with self.subTest(headers=headers):
                    self.assertEqual(raw(url, post(headers=headers))[0], 400)

    def test_truncated_wrong_type_expect_and_oversize_rejected(self):
        with running() as (_, url):
            self.assertEqual(
                raw(url, post().replace(b"Content-Length: 14", b"Content-Length: 100"))[
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
                    post().replace(b"Content-Length: 14", b"Content-Length: 1048577"),
                )[0],
                413,
            )

    def test_query_fields_get_body_and_absolute_form_rejected(self):
        with running() as (_, url):
            for target in (
                "/health?x=1",
                "/health?x=1&x=2",
                "/health?bad=%ZZ",
                "http://example.com/health",
            ):
                request = f"GET {target} HTTP/1.1\r\nHost: local\r\n\r\n".encode()
                self.assertEqual(raw(url, request)[0], 400)
            self.assertEqual(
                raw(
                    url,
                    b"GET /health HTTP/1.1\r\nHost: local\r\nContent-Length: 1\r\n\r\nx",
                )[0],
                400,
            )

    def test_server_errors_and_invalid_results_are_sanitized(self):
        class Broken(Collector):
            def health(self):
                raise RuntimeError("private body")

        with (
            running(Broken()) as (_, url),
            self.assertLogs("metrics_monitor.http", level="WARNING") as logs,
        ):
            with self.assertRaises(HTTPError) as caught:
                http_json(url + "/health")
            self.assertEqual(caught.exception.code, 500)
        self.assertNotIn("private body", "\n".join(logs.output))

        class Invalid(Collector):
            def health(self):
                return {"bad": float("nan")}

        with (
            running(Invalid()) as (_, url),
            self.assertLogs("metrics_monitor.http", level="WARNING"),
        ):
            with self.assertRaises(HTTPError) as caught:
                http_json(url + "/health")
            self.assertEqual(caught.exception.code, 500)

    def test_overload_preserves_slot_until_accepted_work_exits(self):
        entered, release = threading.Event(), threading.Event()

        class Slow(Collector):
            def health(self):
                entered.set()
                release.wait(3)
                return {}

        with (
            running(Slow(), concurrency=1, request_timeout=0.15) as (_, url),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            accepted = executor.submit(http_json, url + "/health", timeout=2)
            try:
                self.assertTrue(entered.wait(1))
                for delay in (0, 0.2):
                    time.sleep(delay)
                    with self.assertRaises(HTTPError) as caught:
                        http_json(
                            url + "/push", "POST", {"samples": ["x" * 65536]}, TOKEN
                        )
                    self.assertEqual(caught.exception.code, 503)
            finally:
                release.set()
            with self.assertRaises(OSError):
                accepted.result(2)

    def test_server_close_drains_accepted_work(self):
        entered, release = threading.Event(), threading.Event()

        class Slow(Collector):
            def health(self):
                entered.set()
                release.wait(3)
                return {}

        with (
            running(Slow()) as (httpd, url),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            accepted = executor.submit(http_json, url + "/health")
            self.assertTrue(entered.wait(1))
            httpd.shutdown()
            close = executor.submit(httpd.server_close)
            try:
                time.sleep(0.06)
                self.assertFalse(close.done())
            finally:
                release.set()
            close.result(2)
            with suppress(OSError):
                accepted.result(2)

    def test_slow_body_has_absolute_deadline(self):
        with (
            running(request_timeout=0.12) as (_, url),
            socket.create_connection(
                ("127.0.0.1", int(url.rsplit(":", 1)[1])), timeout=1
            ) as sock,
        ):
            sock.sendall(post().split(b"\r\n\r\n")[0] + b"\r\n\r\n{")
            started = time.monotonic()
            self.assertEqual(sock.recv(4096), b"")
            self.assertLess(time.monotonic() - started, 0.7)

    def test_slow_headers_have_absolute_deadline(self):
        with (
            running(request_timeout=0.12) as (_, url),
            socket.create_connection(
                ("127.0.0.1", int(url.rsplit(":", 1)[1])), timeout=1
            ) as sock,
        ):
            sock.sendall(b"GET /health HTTP/1.1\r\nHost: ")
            started = time.monotonic()
            self.assertEqual(sock.recv(4096), b"")
            self.assertLess(time.monotonic() - started, 0.7)

    def test_group_by_query_adapter_and_admin_role_boundary(self):
        class Store:
            def query(self, *args, **kwargs):
                self.called = (args, kwargs)
                return {"series": [], "step": 60, "revision": 0}

        class Alerts:
            def put_rule(self, body):
                return body

        store = Store()
        with running(store=store, alerts=Alerts()) as (httpd, url):
            httpd.config["read_token"] = "separate-read-token"
            result = http_json(
                url + "/query?metric=cpu&start=0&end=60&group_by=%5B%22region%22%5D",
                token="separate-read-token",
            )
            self.assertEqual(result["step"], 60)
            self.assertEqual(store.called[1], {"group_by": ["region"]})
            with self.assertRaises(HTTPError) as caught:
                http_json(url + "/rules", "POST", {}, "separate-read-token")
            self.assertEqual(caught.exception.code, 401)
            self.assertEqual(
                http_json(url + "/rules", "POST", {"id": "test"}, TOKEN), {"id": "test"}
            )

    def test_response_framing_size_and_json_validation(self):
        for response in (
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}",
            b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{}",
            b'HTTP/1.1 200 OK\r\nContent-Length: 13\r\n\r\n{"x":1,"x":2}',
            b'HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n{"x":NaN}',
        ):
            with (
                self.subTest(response=response),
                peer(lambda sock: sock.sendall(response)) as url,
            ):
                with self.assertRaises((ValueError, OSError)):
                    http_json(url)
        with peer(
            lambda sock: sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
        ) as url:
            with self.assertRaises(ValueError):
                http_json(url, max_bytes=20)

    def test_chunked_response_and_redirect_refusal(self):
        with peer(
            lambda sock: sock.sendall(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n2\r\n{}\r\n0\r\n\r\n"
            )
        ) as url:
            self.assertEqual(http_json(url), {})
        with peer(
            lambda sock: sock.sendall(
                b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/secret\r\nContent-Length: 0\r\n\r\n"
            )
        ) as url:
            with self.assertRaises(HTTPError) as caught:
                http_json(url, token=TOKEN)
            self.assertEqual(caught.exception.code, 302)

    def test_drip_response_has_absolute_deadline_and_cancellation(self):
        def drip(sock):
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
            for _ in range(20):
                sock.sendall(b" ")
                time.sleep(0.03)

        with peer(drip) as url:
            started = time.monotonic()
            with self.assertRaises((TimeoutError, OSError)):
                http_json(url, timeout=0.12)
            self.assertLess(time.monotonic() - started, 0.7)
        cancel = threading.Event()
        with peer(drip) as url, ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(http_json, url, cancel=cancel, timeout=5)
            time.sleep(0.05)
            cancel.set()
            with self.assertRaises((TimeoutError, OSError)):
                future.result(1)

    def test_configuration_and_huge_numeric_inputs(self):
        for value in (True, float("inf"), float("nan"), 10**10000):
            with self.assertRaises(ValueError):
                number(value)
        for value in (0, True, float("inf"), 10**10000):
            with self.assertRaises(ValueError):
                seconds(value)
        for url in (
            "ftp://local",
            "http://u:p@local",
            "http://local/#secret",
            "http://local:0/",
        ):
            with self.assertRaises(ValueError):
                endpoint(url)
        with self.assertRaises(ValueError):
            loads('{"x":1,"x":2}')
        with self.assertRaises(ValueError):
            server("127.0.0.1", 0, CONFIG, collector=Collector(), concurrency=0)


class PresentationTests(unittest.TestCase):
    def test_exporters_share_owned_tracer_and_close_is_idempotent(self):
        was_tracing = tracemalloc.is_tracing()
        first, second = Exporter("one"), Exporter("two")
        try:
            first.close()
            first.close()
            self.assertTrue(tracemalloc.is_tracing())
            self.assertEqual(second.samples()["samples"][-1]["value"], 1)
            self.assertEqual(second.samples()["samples"][-1]["value"], 2)
            with self.assertRaises(RuntimeError):
                first.samples()
        finally:
            second.close()
        self.assertEqual(tracemalloc.is_tracing(), was_tracing)

    def test_exporter_counter_is_thread_safe(self):
        exporter = Exporter("one", clock=lambda: 100)
        try:
            with ThreadPoolExecutor(max_workers=8) as executor:
                counts = [
                    batch["samples"][-1]["value"]
                    for batch in executor.map(lambda _: exporter.samples(), range(100))
                ]
            self.assertEqual(sorted(counts), list(range(1, 101)))
        finally:
            exporter.close()

    def test_svg_breaks_null_and_missing_buckets_and_escapes_labels(self):
        points = [
            {"timestamp": t, "value": v} for t, v in ((0, 1), (1, None), (2, 2), (5, 3))
        ]
        root = ElementTree.fromstring(
            chart(
                {
                    "series": [
                        {
                            "metric": "a<&",
                            "labels": {"host": "<script>"},
                            "points": points,
                        }
                    ],
                    "step": 1,
                    "revision": "<unsafe>",
                }
            )
        )
        self.assertEqual(len(root.findall("{*}polyline")), 3)
        self.assertEqual(
            root.findall("{*}polyline")[0].find("{*}title").text,
            'a<& {"host":"<script>"}',
        )
        self.assertEqual(len(root.findall("{*}script")), 0)

    def test_svg_budget_rejects_before_rendering(self):
        with self.assertRaises(Error):
            chart({"series": [{"points": [{}] * 20001}], "step": 1, "revision": 0})


if __name__ == "__main__":
    unittest.main()
