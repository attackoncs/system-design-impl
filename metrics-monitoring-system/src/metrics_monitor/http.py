"""Bounded authenticated metrics API, with independent presentation helpers."""

from contextlib import suppress
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
from pathlib import Path
import re
import socket
import sqlite3
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .common import Error, dumps
from .presentation import Exporter as Exporter
from .presentation import chart as chart
from .transport import Deadline, MAX_BODY, length, loads, seconds, secret

_LOG = logging.getLogger(__name__)
MAX_REQUEST = 1024 * 1024


class _Server(ThreadingHTTPServer):
    daemon_threads = False
    request_queue_size = 128

    def __init__(
        self,
        address,
        config,
        services,
        *,
        concurrency=32,
        request_timeout=10,
        workers=None,
    ):
        if type(concurrency) is not int or not 1 <= concurrency <= 256:
            raise ValueError("HTTP capacity must be in 1..256")
        config = loads(dumps(config))
        for key in ("read_token", "admin_token", "push_token", "exporter_token"):
            if key in config and not secret(config[key]):
                raise ValueError("private credential required")
        self.config, self.services, self.workers = config, services, workers or {}
        self.request_timeout = seconds(request_timeout)
        self.slots = threading.BoundedSemaphore(concurrency)
        self.stopping, self._local = threading.Event(), threading.local()
        super().__init__(address, _Handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(min(5, self.request_timeout))
        return request, address

    def process_request(self, request, address):
        if self.stopping.is_set() or not self.slots.acquire(False):
            try:
                request.settimeout(0.1)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                    b"Cache-Control: no-store\r\nConnection: close\r\n\r\n"
                )
                request.shutdown(socket.SHUT_WR)
                # request() can transmit headers/body in separate TCP writes.
                # Closing after one recv can leave the body unread and turn an
                # already-sent 503 into a reset on Windows. Give the peer time
                # to receive the rejection and close, while bounding accept-loop
                # work by both elapsed time and the reference input ceiling.
                deadline = time.monotonic() + 0.1
                drained = 0
                while drained <= MAX_BODY + 65536:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    request.settimeout(remaining)
                    part = request.recv(min(65536, MAX_BODY + 65537 - drained))
                    if not part:
                        break
                    drained += len(part)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        cancelled = self._local.cancelled = threading.Event()
        self._local.deadline = time.monotonic() + self.request_timeout

        def interrupt():
            cancelled.set()
            with suppress(OSError):
                request.shutdown(socket.SHUT_RDWR)

        try:
            with Deadline(self._local.deadline, interrupt, self.stopping):
                super().process_request_thread(request, address)
        finally:
            del self._local.cancelled
            del self._local.deadline
            self.slots.release()

    def handle_error(self, request, address):
        cancelled = getattr(self._local, "cancelled", None)
        if (
            cancelled is not None
            and cancelled.is_set()
            and isinstance(sys.exc_info()[1], OSError)
        ):
            return
        _LOG.warning("metrics HTTP handler failed")

    def server_close(self):
        self.stopping.set()
        super().server_close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def handle_expect_100(self):
        return True

    def _live(self):
        if (
            self.server._local.cancelled.is_set()
            or time.monotonic() >= self.server._local.deadline
        ):
            raise TimeoutError("request expired")
        self.connection.settimeout(
            max(0.001, min(5, self.server._local.deadline - time.monotonic()))
        )

    def _header(self, key):
        values = self.headers.get_all(key, [])
        if len(values) > 1:
            raise ValueError("duplicate header")
        return values[0] if values else None

    def auth(self, role):
        actual = (self._header("Authorization") or "").encode()
        keys = [role + "_token", "admin_token"] if role != "admin" else ["admin_token"]
        for key in keys:
            token = self.server.config.get(key)
            if token and hmac.compare_digest(actual, ("Bearer " + token).encode()):
                return
        raise Error(401, "credential required for role")

    def body(self):
        if self._header("Transfer-Encoding") is not None:
            raise ValueError("Transfer-Encoding unsupported")
        if self._header("Expect") is not None:
            raise Error(417, "Expect unsupported")
        size = length(self._header("Content-Length"))
        if not 0 < size <= MAX_REQUEST:
            raise Error(413, "body must be 1 byte..1 MiB")
        kind = self._header("Content-Type")
        if kind and kind.split(";", 1)[0].strip().lower() != "application/json":
            raise Error(415, "JSON content type required")
        self._live()
        payload = self.rfile.read(size)
        self._live()
        if len(payload) != size:
            raise ValueError("truncated request body")
        self._body_read = True
        result = loads(payload)
        if not isinstance(result, dict):
            raise ValueError("JSON object required")
        return result

    def dispatch(self):
        if len(self.path) > 8192 or re.search(r"%(?![0-9a-fA-F]{2})", self.path):
            raise ValueError("invalid request target")
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise ValueError("relative request target required")
        params = parse_qs(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            errors="strict",
            max_num_fields=16,
        )
        if any(len(values) != 1 for values in params.values()):
            raise ValueError("duplicate query field")
        params = {key: values[0] for key, values in params.items()}
        path = parsed.path
        store, alerts, collector, exporter = (
            self.server.services[key]
            for key in ("store", "alerts", "collector", "exporter")
        )
        if self.command == "GET":
            if (
                self._header("Transfer-Encoding") is not None
                or self._header("Expect") is not None
            ):
                raise ValueError("GET framing unsupported")
            size = self._header("Content-Length")
            if size is not None and length(size) != 0:
                raise ValueError("GET body unsupported")
        if path not in ("/query", "/chart.svg") and params:
            raise ValueError("unexpected query fields")
        if self.command == "GET" and path == "/health":
            result = (
                {"role": "exporter"}
                if exporter
                else (
                    {"role": "collector", **collector.health()}
                    if collector
                    else {"role": "query", **store.health()}
                )
            )
            return {
                **result,
                "background": {
                    key: worker.last_error
                    for key, worker in self.server.workers.items()
                },
            }
        if exporter and path == "/metrics" and self.command == "GET":
            self.auth("exporter")
            return exporter.samples()
        if collector and path == "/push" and self.command == "POST":
            self.auth("push")
            body = self.body()
            if set(body) != {"samples"}:
                raise ValueError("samples required")
            return collector.enqueue(body["samples"], self._header("Idempotency-Key"))
        if store and self.command == "GET" and path == "/dashboard":
            return Path(__file__).with_name(
                "dashboard.html"
            ).read_bytes(), "text/html; charset=utf-8"
        if store and self.command == "GET" and path in ("/query", "/chart.svg"):
            self.auth("read")
            if not {"metric", "start", "end"} <= set(params) or set(params) - {
                "metric",
                "start",
                "end",
                "match",
                "step",
                "aggregation",
                "group_by",
            }:
                raise ValueError("invalid query fields")
            options = {}
            if "group_by" in params:
                options["group_by"] = loads(params["group_by"])
            result = store.query(
                params["metric"],
                float(params["start"]),
                float(params["end"]),
                loads(params.get("match", "{}")),
                int(params["step"]) if "step" in params else None,
                params.get("aggregation", "avg"),
                **options,
            )
            return (chart(result), "image/svg+xml") if path == "/chart.svg" else result
        if alerts and self.command == "GET" and path == "/alerts":
            self.auth("read")
            return alerts.status()
        if (
            alerts
            and self.command == "POST"
            and path in ("/rules", "/silences", "/evaluate")
        ):
            self.auth("admin")
            body = self.body()
            if path == "/rules":
                return alerts.put_rule(body)
            if path == "/silences":
                return alerts.silence(**body)
            if body:
                raise ValueError("evaluate body must be empty")
            return alerts.evaluate()
        if store and path == "/compact" and self.command == "POST":
            self.auth("admin")
            if self.body():
                raise ValueError("compact body must be empty")
            store.compact()
            return store.health()
        raise Error(404, "route not found")

    def handle_request(self):
        self.close_connection = True
        self._body_read = False
        try:
            result = self.dispatch()
            try:
                payload, kind = (
                    result
                    if isinstance(result, tuple)
                    else (dumps(result).encode(), "application/json")
                )
            except (ValueError, TypeError, OverflowError, RecursionError):
                raise RuntimeError("invalid response") from None
            if not isinstance(payload, bytes) or not isinstance(kind, str):
                raise RuntimeError("invalid response")
            status = 200
        except Error as exc:
            status, payload, kind = (
                exc.status,
                dumps({"error": exc.message}).encode(),
                "application/json",
            )
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            status, payload, kind = (
                400,
                b'{"error":"invalid request"}',
                "application/json",
            )
        except (OSError, sqlite3.Error):
            status, payload, kind = (
                503,
                b'{"error":"storage or peer unavailable"}',
                "application/json",
            )
        except Exception:
            _LOG.warning("metrics request failed")
            status, payload, kind = (
                500,
                b'{"error":"internal error"}',
                "application/json",
            )
        if len(payload) > MAX_BODY:
            status, payload, kind = (
                503,
                b'{"error":"response exceeds local limit"}',
                "application/json",
            )
        try:
            self._live()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            if not self._body_read and self.command == "POST":
                self.connection.shutdown(socket.SHUT_WR)
                deadline = min(self.server._local.deadline, time.monotonic() + 0.1)
                remaining_bytes = MAX_REQUEST + 65536
                while remaining_bytes > 0 and time.monotonic() < deadline:
                    self.connection.settimeout(max(0.001, deadline - time.monotonic()))
                    chunk = self.rfile.read1(min(65536, remaining_bytes))
                    if not chunk:
                        break
                    remaining_bytes -= len(chunk)
        except OSError:
            pass

    do_GET = do_POST = handle_request


def server(
    host,
    port,
    config,
    store=None,
    alerts=None,
    collector=None,
    exporter=None,
    *,
    concurrency=32,
    request_timeout=10,
    workers=None,
):
    services = dict(store=store, alerts=alerts, collector=collector, exporter=exporter)
    if all(value is None for value in services.values()):
        raise ValueError("at least one service required")
    return _Server(
        (host, port),
        config,
        services,
        concurrency=concurrency,
        request_timeout=request_timeout,
        workers=workers,
    )
