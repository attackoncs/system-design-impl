from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError

from test_transport_reliability import running, raw_request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from lab import configuration
from payment_system.common import http_json
from payment_system.http import server
from payment_system.store import Store


class HTTPReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = configuration([9870, 9871, 9872])
        self.store = Store(Path(self.temp.name) / "store.sqlite", ("seller", "other"))

    def instance(self, **kwargs):
        return server("api", self.config, self.store, "127.0.0.1", 0, **kwargs)

    def raw(self, path="/v1/payments", body=b"{}", headers=b"", method="POST"):
        auth = self.config["merchants"]["seller"]
        return (
            f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {auth}\r\nContent-Type: application/json\r\n".encode()
            + headers
            + b"\r\n"
            + body
        )

    def test_unknown_queries_exact_paths_and_origin_form(self):
        with running(self.instance()) as instance:
            for path, status in (
                ("/v1/wallet?ignored=1", 400),
                ("/v1/operations/a/extra", 404),
                ("//outside/v1/wallet", 400),
                ("/v1/%77allet", 400),
            ):
                raw = self.raw(path, b"", method="GET")
                self.assertIn(
                    str(status).encode(), raw_request(instance, raw).split(b"\r\n")[0]
                )

    def test_ambiguous_or_incomplete_body_never_creates_payment(self):
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
        self.assertEqual(self.store.status()["operations"], 0)

    def test_duplicate_nonfinite_and_nonobject_json_fail(self):
        with running(self.instance()) as instance:
            for body in (b"[]", b'{"amount":1,"amount":2}', b'{"amount":NaN}'):
                raw = self.raw(
                    body=body, headers=f"Content-Length: {len(body)}\r\n".encode()
                )
                self.assertIn(b" 400 ", raw_request(instance, raw).split(b"\r\n")[0])

    def test_internal_checkout_rejects_merchant_credentials(self):
        with running(self.instance()) as instance:
            with self.assertRaises(HTTPError) as caught:
                http_json(
                    f"http://127.0.0.1:{instance.server_port}/internal/checkouts",
                    self.config["merchants"]["seller"],
                    {},
                )
            self.assertEqual(caught.exception.code, 401)
            caught.exception.close()

    def test_claim_and_admin_contracts_do_not_ignore_fields(self):
        with running(self.instance()) as instance:
            for path, role in (
                ("/internal/claim", "internal"),
                ("/admin/reconcile", "admin"),
            ):
                with self.assertRaises(HTTPError) as caught:
                    http_json(
                        f"http://127.0.0.1:{instance.server_port}" + path,
                        self.config[role + "_token"],
                        {"ignored": True},
                    )
                self.assertEqual(caught.exception.code, 400)
                caught.exception.close()

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
                http_json(f"http://127.0.0.1:{instance.server_port}/health")[
                    "simulated_payments_only"
                ]
            )
