from pathlib import Path
import socket
import sys
import time
from helpers_review import StoreCase, configuration
from scale_from_zero.common import Error

from test_transport_reliability import running, raw_request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from scale_from_zero.common import request as http_json
from scale_from_zero.http import server


class HTTPReliabilityTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.config = configuration("single")
        from scale_from_zero.app import App

        self.app = App(self.config, "single", self.store)

    def instance(self, **kwargs):
        return server("single", self.app, self.config, 0, "127.0.0.1", 0, **kwargs)

    def raw(self, path="/v1/login", body=b"{}", headers=b"", method="POST"):
        auth = self.config["admin_token"]
        return (
            f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {auth}\r\nContent-Type: application/json\r\n".encode()
            + headers
            + b"\r\n"
            + body
        )

    def test_unknown_queries_exact_paths_and_origin_form(self):
        with running(self.instance()) as instance:
            for path, status in (
                ("/v1/profile?ignored=1", 400),
                ("/v1/profile/extra", 404),
                ("//outside/v1/wallet", 400),
                ("/v1/%77allet", 400),
            ):
                raw = self.raw(path, b"", method="GET")
                self.assertIn(
                    str(status).encode(), raw_request(instance, raw).split(b"\r\n")[0]
                )

    def test_ambiguous_or_incomplete_body_never_creates_profile(self):
        with running(self.instance()) as instance:
            for headers in (
                b"Content-Length: 2\r\nContent-Length: 2\r\n",
                b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n",
                b"Content-Length: +2\r\n",
                b"Content-Length: 20\r\n",
            ):
                self.assertIn(
                    b" 400 ",
                    raw_request(instance, self.raw(headers=headers)).split(b"\r\n")[0],
                )
        self.assertEqual(self.store.status()["seq"], 0)

    def test_duplicate_nonfinite_and_nonobject_json_fail(self):
        with running(self.instance()) as instance:
            for body in (b"[]", b'{"amount":1,"amount":2}', b'{"amount":NaN}'):
                raw = self.raw(
                    body=body, headers=f"Content-Length: {len(body)}\r\n".encode()
                )
                self.assertIn(b" 400 ", raw_request(instance, raw).split(b"\r\n")[0])

    def test_idle_request_is_cancelled_and_capacity_released(self):
        with running(self.instance(concurrency=1, request_timeout=0.15)) as instance:
            with socket.create_connection(instance.server_address, timeout=2) as slow:
                slow.sendall(b"GET /health HTTP/1.1\r\n")
                time.sleep(0.03)
                response = raw_request(
                    instance, b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n"
                )
                self.assertIn(b" 503 ", response.split(b"\r\n")[0])
                self.assertEqual(slow.recv(1), b"")
            time.sleep(0.03)
            self.assertTrue(
                http_json(
                    f"http://127.0.0.1:{instance.server_port}/health",
                    self.config["internal_token"],
                )["single_host_lab"]
            )

    def test_duplicate_authorization_and_nonempty_admin_command_are_rejected(self):
        with running(self.instance()) as instance:
            duplicate = self.raw(
                body=b"{}",
                headers=(
                    "Authorization: Bearer "
                    + self.config["admin_token"]
                    + "\r\nContent-Length: 2\r\n"
                ).encode(),
            )
            self.assertIn(b" 401 ", raw_request(instance, duplicate).split(b"\r\n")[0])
            with self.assertRaises(Error) as caught:
                http_json(
                    f"http://127.0.0.1:{instance.server_port}/admin/status",
                    self.config["admin_token"],
                    {"ignored": True},
                )
            self.assertEqual(caught.exception.status, 405)

    def test_queries_are_unique_bounded_and_route_specific(self):
        with running(self.instance()) as instance:
            for path in (
                "/v1/profile?min_version=1&min_version=2",
                "/v1/profile?min_version=NaN",
                "/v1/profile?consistency=latest",
                "/health?unknown=1",
            ):
                self.assertIn(
                    b" 400 ",
                    raw_request(instance, self.raw(path, b"", method="GET")).split(
                        b"\r\n"
                    )[0],
                )
