"""Bounded authenticated ingress, worker and query HTTP APIs."""

from contextlib import suppress
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
import re
import socket
import sqlite3
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlsplit

from .common import Error, dumps
from .lifecycle import WorkerLoop as WorkerLoop
from .service import reconcile
from .transport import Deadline, MAX_BODY, length, loads, seconds, secret

_LOG = logging.getLogger(__name__)
MAX_REQUEST = 1024 * 1024


class _Server(ThreadingHTTPServer):
    daemon_threads = False
    request_queue_size = 128

    def __init__(
        self,
        address,
        role,
        config,
        store,
        loop,
        *,
        concurrency=32,
        request_timeout=10,
        queries=None,
    ):
        if type(concurrency) is not int or not 1 <= concurrency <= 256:
            raise ValueError("HTTP capacity must be in 1..256")
        config = loads(dumps(config))
        for key in ("read_token", "admin_token", "write_token", "internal_token"):
            if key in config and not secret(config[key]):
                raise ValueError("private credential required")
        self.role, self.config, self.store, self.loop = role, config, store, loop
        self.queries = queries
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
        _LOG.warning("ad click HTTP handler failed")

    def server_close(self):
        self.stopping.set()
        try:
            super().server_close()
        finally:
            if self.queries is not None:
                self.queries.close()


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

    def auth(self, *roles):
        actual = (self._header("Authorization") or "").encode()
        for role in roles + ("admin",):
            token = self.server.config.get(role + "_token")
            if token and hmac.compare_digest(actual, ("Bearer " + token).encode()):
                return
        raise Error(401, "valid bearer credential required")

    def body(self, *, allow_empty=False):
        if self._header("Transfer-Encoding") is not None:
            raise ValueError("Transfer-Encoding unsupported")
        if self._header("Expect") is not None:
            raise Error(417, "Expect unsupported")
        size = length(self._header("Content-Length"))
        if allow_empty and size == 0:
            self._body_read = True
            return {}
        if not 0 < size <= MAX_REQUEST:
            raise Error(413, "JSON request must contain 1..1048576 bytes")
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
        target = urlsplit(self.path)
        if target.scheme or target.netloc or target.fragment:
            raise ValueError("relative request target required")
        path = target.path
        params = parse_qs(
            target.query,
            keep_blank_values=True,
            strict_parsing=True,
            errors="strict",
            max_num_fields=16,
        )
        if any(len(values) != 1 for values in params.values()):
            raise ValueError("repeated query parameter")
        params = {key: values[0] for key, values in params.items()}
        post = self.command == "POST"
        if not post:
            if (
                self._header("Transfer-Encoding") is not None
                or self._header("Expect") is not None
            ):
                raise ValueError("GET framing unsupported")
            size = self._header("Content-Length")
            if size is not None and length(size) != 0:
                raise ValueError("GET body unsupported")
        if params and path != "/internal/log" and not path.startswith("/v1/ads/"):
            raise ValueError("unexpected query parameters")
        role, config, store, loop = (
            self.server.role,
            self.server.config,
            self.server.store,
            self.server.loop,
        )
        if path == "/health" and not post:
            return {"role": role, "ready": loop is None or loop.error is None}
        if role == "ingress":
            if path == "/events" and post:
                self.auth("write")
                body = self.body()
                if set(body) != {"events"}:
                    raise ValueError("expected events object")
                return store.append(body["events"])
            if path == "/internal/log" and not post:
                self.auth("internal")
                if set(params) - {"partition", "after", "limit", "until"}:
                    raise ValueError("unknown log parameters")
                return store.read(**{key: int(value) for key, value in params.items()})
            if path == "/internal/status" and not post:
                self.auth("internal")
                return store.status()
        if role == "worker":
            if path == "/internal/status" and not post:
                self.auth("internal")
                return {
                    **store.status(),
                    "error": loop.error if loop else None,
                    "last_poll": loop.updated if loop else 0,
                }
            if path == "/internal/totals" and post:
                self.auth("internal")
                if loop is not None and loop.error:
                    raise Error(503, "aggregation worker not ready")
                return store.totals(self.body())
            if path == "/internal/audit" and not post:
                self.auth("internal")
                if loop is not None and loop.error:
                    raise Error(503, "aggregation worker not ready")
                return store.audit()
        if role == "api":
            if path == "/admin/reconcile" and post:
                self.auth("admin")
                if self.body(allow_empty=True):
                    raise ValueError("reconcile body must be empty")
                return reconcile(config)
            if path == "/query" and post:
                self.auth("read")
                return self.server.queries.aggregate(self.body())
            if not post and path.startswith("/v1/ads/"):
                self.auth("read")
                if set(params) - {
                    "from",
                    "to",
                    "window",
                    "count",
                    "user_id",
                    "ip",
                    "country",
                }:
                    raise ValueError("unknown query parameter")
                body = {
                    key: int(value)
                    for key, value in params.items()
                    if key in {"from", "to", "window", "count"}
                }
                body["filters"] = {
                    key: value
                    for key, value in params.items()
                    if key in {"user_id", "ip", "country"}
                }
                if path == "/v1/ads/popular_ads":
                    return self.server.queries.aggregate(body)
                suffix = "/aggregated_count"
                if path.endswith(suffix):
                    body["ad_id"] = unquote(
                        path[len("/v1/ads/") : -len(suffix)], errors="strict"
                    )
                    return self.server.queries.aggregate(body)
        raise Error(404, "unknown route or method")

    def handle_request(self):
        self.close_connection = True
        self._body_read = False
        try:
            result, status = self.dispatch(), 200
        except Error as exc:
            result, status = {"error": str(exc)}, exc.status
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            result, status = {"error": "invalid request"}, 400
        except HTTPError as exc:
            status = 422 if exc.code == 422 else 503
            result = {
                "error": "candidate budget exceeded"
                if status == 422
                else "required upstream unavailable"
            }
            exc.close()
        except (OSError, sqlite3.Error):
            result, status = {"error": "required upstream unavailable"}, 503
        except Exception:
            _LOG.warning("ad click request failed")
            result, status = {"error": "internal error; input position retained"}, 500
        try:
            payload = dumps(result).encode()
        except (ValueError, TypeError, OverflowError, RecursionError):
            _LOG.warning("ad click response serialization failed")
            payload, status = b'{"error":"internal error"}', 500
        if len(payload) > MAX_BODY:
            payload, status = b'{"error":"response exceeds local limit"}', 503
        try:
            self._live()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            if not self._body_read and self.command == "POST":
                self.connection.shutdown(socket.SHUT_WR)
                deadline = min(self.server._local.deadline, time.monotonic() + 0.1)
                remaining = MAX_REQUEST + 65536
                while remaining > 0 and time.monotonic() < deadline:
                    self.connection.settimeout(max(0.001, deadline - time.monotonic()))
                    chunk = self.rfile.read1(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
        except OSError:
            pass

    do_GET = do_POST = handle_request


def server(
    role,
    config,
    store,
    host,
    port,
    loop=None,
    *,
    concurrency=32,
    request_timeout=10,
    query_workers=None,
    query_capacity=None,
    query_timeout=None,
):
    if role not in ("ingress", "worker", "api"):
        raise ValueError("unknown service role")
    queries = None
    if role == "api":
        from .service import QueryService

        queries = QueryService(
            config,
            workers=config.get("query_workers", 8)
            if query_workers is None
            else query_workers,
            capacity=config.get("query_capacity", 8)
            if query_capacity is None
            else query_capacity,
            timeout=config.get("query_timeout", 5)
            if query_timeout is None
            else query_timeout,
        )
    try:
        return _Server(
            (host, port),
            role,
            config,
            store,
            loop,
            concurrency=concurrency,
            request_timeout=request_timeout,
            queries=queries,
        )
    except BaseException:
        if queries is not None:
            queries.close()
        raise
