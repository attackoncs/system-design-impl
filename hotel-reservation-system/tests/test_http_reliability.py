"""Actual localhost HTTP/RPC with controlled services and raw peer faults."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
import json
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
from hotel_reservation.common import Error, http_json
from hotel_reservation.http import server
from hotel_reservation.routes import public_route
from hotel_reservation.transport import endpoint, loads, seconds

CONFIG = {role + "_token": (role[0] * 32) for role in ("admin", "internal", "payment")}
CONFIG.update(users={"alice": "u" * 32}, shard_urls=["http://127.0.0.1:1"])
ADMIN, USER = CONFIG["admin_token"], CONFIG["users"]["alice"]


class Store:
    def __init__(self):
        self.calls = []

    def owner(self, hotel):
        return None

    def catalog(self, hotel, room=None):
        self.calls.append((hotel, room))
        return {"hotel": hotel, "room": room}

    def manage(self, hotel, body, *args, **kwargs):
        self.calls.append(body)
        return {"updated": True}


@contextmanager
def running(store=None, role="shard", **kwargs):
    httpd = server(
        role, CONFIG, store or Store(), "127.0.0.1", 0, calendar=Mock(), **kwargs
    )
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


def raw(url, message):
    with socket.create_connection(
        ("127.0.0.1", int(url.rsplit(":", 1)[1])), timeout=2
    ) as sock:
        sock.sendall(message)
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            part = sock.recv(65536)
            if not part:
                break
            chunks.append(part)
    response = b"".join(chunks)
    return int(response.split(b" ", 2)[1]), response


def request(
    path="/v1/hotels/h",
    method="PUT",
    body=b'{"name":"hotel","address":"here"}',
    token=ADMIN,
    headers=(),
):
    values = [
        ("Host", "local"),
        ("Authorization", "Bearer " + token),
        ("Content-Length", str(len(body))),
        *headers,
    ]
    return (
        f"{method} {path} HTTP/1.1\r\n".encode()
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


class HTTPTests(unittest.TestCase):
    def test_catalog_and_health_actual_http(self):
        with running() as (_httpd, url):
            self.assertEqual(http_json(url + "/health", USER)["role"], "shard")
            self.assertEqual(
                http_json(url + "/v1/hotels/h", USER), {"hotel": "h", "room": None}
            )
            self.assertTrue(
                http_json(
                    url + "/v1/hotels/h", ADMIN, {"name": "h", "address": "a"}, "PUT"
                )["updated"]
            )

    def test_role_authentication_precedes_waiting_for_body(self):
        with running() as (_httpd, url):
            status, _ = raw(
                url,
                b"PUT /v1/hotels/h HTTP/1.1\r\nHost: local\r\nAuthorization: Bearer "
                + USER.encode()
                + b"\r\nContent-Length: 900000\r\n\r\n",
            )
            self.assertEqual(status, 401)

    def test_guest_required_and_duplicate_auth_rejected(self):
        with running() as (_httpd, url):
            self.assertEqual(
                raw(url, request("/v1/reservations", "POST", b"{}"))[0], 400
            )
            self.assertEqual(
                raw(url, request(headers=[("Authorization", "Bearer " + ADMIN)]))[0],
                400,
            )

    def test_framing_json_and_unknown_queries_are_strict(self):
        cases = [
            request(headers=[("Content-Length", "1")]),
            request(headers=[("Transfer-Encoding", "chunked")]),
            request(body=b'{"name":"a","name":"b","address":"c"}'),
            request(body=b'{"name":"a","address":NaN}'),
            request("/v1/hotels/h?unexpected=1"),
            request(headers=[("Content-Type", "text/plain")]),
        ]
        with running() as (_httpd, url):
            for message in cases:
                status, _ = raw(url, message)
                self.assertIn(status, (400, 415))

    def test_get_body_and_expect_rejected(self):
        with running() as (_httpd, url):
            self.assertEqual(raw(url, request(method="GET", token=USER))[0], 400)
            self.assertEqual(
                raw(url, request(headers=[("Expect", "100-continue")]))[0], 417
            )

    def test_gateway_explicit_routes_never_forward_internal_or_suffixes(self):
        with (
            running(role="gateway") as (_httpd, url),
            patch("hotel_reservation.http.http_json") as forward,
        ):
            for path in (
                "/internal/status",
                "/v1/hotels/h/private",
                "/v1/reservations/a.extra/private",
            ):
                self.assertEqual(raw(url, request(path, "GET", b"", USER))[0], 404)
            self.assertEqual(
                raw(url, request("/v1/hotels/h", "PUT", token=USER))[0], 401
            )
            forward.assert_not_called()

    def test_gateway_forwards_one_write_with_original_identity_and_budget(self):
        with (
            running(role="gateway") as (_httpd, url),
            patch(
                "hotel_reservation.http.http_json", return_value={"updated": True}
            ) as forward,
        ):
            result = http_json(
                url + "/v1/hotels/h", ADMIN, {"name": "n", "address": "a"}, "PUT"
            )
            self.assertTrue(result["updated"])
            args, kwargs = forward.call_args
            self.assertEqual(
                args[:4],
                (
                    "http://127.0.0.1:1/v1/hotels/h",
                    ADMIN,
                    {"name": "n", "address": "a"},
                    "PUT",
                ),
            )
            self.assertIn("deadline", kwargs)
            self.assertIn("cancel", kwargs)
            self.assertEqual(forward.call_count, 1)

    def test_gateway_preserves_safe_error_status_without_peer_body(self):
        with (
            running(role="gateway") as (_httpd, url),
            patch(
                "hotel_reservation.http.http_json",
                side_effect=HTTPError("unused", 409, "private body", {}, None),
            ) as forward,
        ):
            status, response = raw(url, request())
            self.assertEqual(status, 409)
            self.assertNotIn(b"private body", response)
            self.assertEqual(forward.call_count, 1)

    def test_server_copies_configuration(self):
        config = json.loads(json.dumps(CONFIG))
        httpd = server("shard", config, Store(), "127.0.0.1", 0, calendar=Mock())
        try:
            config["users"]["alice"] = "changed"
            config["shard_urls"].clear()
            self.assertEqual(httpd.config["users"]["alice"], USER)
            self.assertEqual(len(httpd.config["shard_urls"]), 1)
        finally:
            httpd.server_close()

    def test_capacity_is_retained_until_accepted_work_exits(self):
        entered, release = threading.Event(), threading.Event()

        class Slow(Store):
            def catalog(self, hotel, room=None):
                entered.set()
                release.wait(2)
                return {}

        with (
            running(Slow(), concurrency=1, request_timeout=0.15) as (_httpd, url),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            accepted = executor.submit(http_json, url + "/v1/hotels/h", USER, timeout=2)
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaises(HTTPError) as error:
                    http_json(
                        url + "/v1/hotels/h", ADMIN, {"padding": "x" * 65536}, "PUT"
                    )
                self.assertEqual(error.exception.code, 503)
                time.sleep(0.2)
                with self.assertRaises(HTTPError) as error:
                    http_json(url + "/v1/hotels/h", USER)
                self.assertEqual(error.exception.code, 503)
            finally:
                release.set()
            with self.assertRaises(OSError):
                accepted.result(2)

    def test_close_drains_application_work(self):
        entered, release, closed = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        class Slow(Store):
            def catalog(self, hotel, room=None):
                entered.set()
                release.wait(2)
                return {}

        with (
            running(Slow()) as (httpd, url),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            accepted = executor.submit(http_json, url + "/v1/hotels/h", USER)
            try:
                self.assertTrue(entered.wait(1))
                httpd.shutdown()
                closing = executor.submit(lambda: (httpd.server_close(), closed.set()))
                self.assertFalse(closed.wait(0.05))
            finally:
                release.set()
            with suppress(OSError):
                accepted.result(2)
            closing.result(2)

    def test_request_deadline_bounds_slow_headers_and_body(self):
        with running(request_timeout=0.1) as (_httpd, url):
            for initial in (
                b"PUT /v1/hotels/h HTTP/1.1\r\n",
                b"PUT /v1/hotels/h HTTP/1.1\r\nHost: local\r\nAuthorization: Bearer "
                + ADMIN.encode()
                + b"\r\nContent-Length: 100\r\n\r\n{",
            ):
                with socket.create_connection(
                    ("127.0.0.1", int(url.rsplit(":", 1)[1])), timeout=1
                ) as sock:
                    sock.sendall(initial)
                    started = time.monotonic()
                    self.assertEqual(sock.recv(1024), b"")
                    self.assertLess(time.monotonic() - started, 0.8)

    def test_route_contract_limits_query_fields(self):
        route, params = public_route(
            "/v1/hotels/h/availability?start=2030-01-01&end=2030-01-02&quantity=2",
            "GET",
        )
        self.assertEqual(route.operation, "hotel_availability")
        self.assertEqual(params["quantity"], "2")
        for path in (
            "/v1/hotels/h/availability?start=x",
            "/v1/hotels/h?x=1",
            "/v1/reservations?hotel=h&hotel=b",
        ):
            with self.assertRaises((Error, ValueError)):
                public_route(path, "GET")

    def test_rpc_ignores_environment_proxies_and_rejects_redirect(self):
        with (
            peer(
                lambda sock: sock.sendall(
                    b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\nContent-Type: application/json\r\n\r\n{"ok":true}'
                )
            ) as url,
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
            self.assertEqual(http_json(url, USER), {"ok": True})
        with (
            peer(
                lambda sock: sock.sendall(
                    b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1\r\nContent-Length: 0\r\n\r\n"
                )
            ) as url,
            self.assertRaises(HTTPError) as caught,
        ):
            http_json(url, USER)
        self.assertEqual(caught.exception.code, 302)

    def test_rpc_rejects_ambiguous_truncated_and_nonfinite_responses(self):
        responses = [
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n{}",
            b'HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\n{"x":NaN}',
        ]
        for response in responses:
            with (
                peer(lambda sock: sock.sendall(response)) as url,
                self.assertRaises((ValueError, OSError)),
            ):
                http_json(url, USER)

    def test_rpc_absolute_deadline_and_cancellation(self):
        def drip(sock):
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
            for _ in range(30):
                sock.sendall(b" ")
                time.sleep(0.03)

        with peer(drip) as url:
            started = time.monotonic()
            with self.assertRaises((ValueError, OSError)):
                http_json(url, USER, timeout=0.1)
            self.assertLess(time.monotonic() - started, 0.8)
        cancel = threading.Event()
        with (
            peer(
                lambda sock: (
                    cancel.wait(1),
                    sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"),
                )
            ) as url,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            result = executor.submit(http_json, url, USER, cancel=cancel)
            cancel.set()
            with self.assertRaises(OSError):
                result.result(1)

    def test_numeric_url_and_json_configuration_validation(self):
        for value in (float("nan"), float("inf"), True, 0, 10**1000):
            with self.assertRaises(ValueError):
                seconds(value)
        for url in (
            "ftp://host",
            "http://user:password@host",
            "http://host/#fragment",
            "http://host:0",
        ):
            with self.assertRaises(ValueError):
                endpoint(url)
        for raw in ('{"x":1,"x":2}', '{"x":1e999}', '{"x":NaN}'):
            with self.assertRaises(ValueError):
                loads(raw)


if __name__ == "__main__":
    unittest.main()
