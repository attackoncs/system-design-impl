"""Authenticated JSON RPC with bounded admission and owned handler shutdown."""

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

from .common import Error, dumps
from .transport import Deadline, MAX_BODY, length, loads, seconds, secret

_LOG = logging.getLogger(__name__)


class _Server(ThreadingHTTPServer):
    daemon_threads = False
    request_queue_size = 128

    def __init__(
        self,
        address,
        service,
        token,
        *,
        concurrency=32,
        request_timeout=10,
        maintenance=None,
    ):
        if type(concurrency) is not int or not 1 <= concurrency <= 256:
            raise ValueError("HTTP capacity must be in 1..256")
        if not secret(token):
            raise ValueError("private credential required")
        self.service, self.token, self.maintenance = service, token, maintenance
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
        _LOG.warning("queue HTTP handler failed")

    def server_close(self):
        self.stopping.set()
        super().server_close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def handle_expect_100(self):
        # Let do_POST authenticate before rejecting an unsupported expectation.
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

    def _header(self, name):
        values = self.headers.get_all(name, [])
        if len(values) > 1:
            raise ValueError("duplicate header")
        return values[0] if values else None

    def _dispatch(self):
        credential = self._header("Authorization") or ""
        if not hmac.compare_digest(
            credential.encode(), ("Bearer " + self.server.token).encode()
        ):
            raise Error(401, "credential required")
        if self._header("Transfer-Encoding") is not None:
            raise ValueError("Transfer-Encoding unsupported")
        if self._header("Expect") is not None:
            raise Error(417, "Expect unsupported")
        size = length(self._header("Content-Length"))
        if not 0 < size <= MAX_BODY:
            raise Error(413, "body must be 1 byte..32 MiB")
        content_type = self._header("Content-Type")
        if (
            content_type
            and content_type.split(";", 1)[0].strip().lower() != "application/json"
        ):
            raise Error(415, "JSON content type required")
        if not re.fullmatch(r"/[a-z][a-z-]{0,63}", self.path):
            raise ValueError("invalid RPC target")
        self._live()
        data = self.rfile.read(size)
        self._live()
        if len(data) != size:
            raise ValueError("truncated request body")
        body = loads(data)
        if not isinstance(body, dict):
            raise ValueError("object required")
        result = self.server.service.dispatch(self.path, body)
        if (
            self.path in ("/health", "/metadata")
            and self.server.maintenance is not None
        ):
            result = {
                **result,
                "maintenance_error": self.server.maintenance.last_error
                or getattr(self.server.service, "last_error", None),
            }
        return result

    def do_POST(self):
        self.close_connection = True
        try:
            result, status = self._dispatch(), 200
        except Error as exc:
            result, status = {"error": exc.message}, exc.status
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            result, status = {"error": "invalid request"}, 400
        except (OSError, sqlite3.Error):
            result, status = {"error": "storage or peer unavailable"}, 503
        except Exception:
            _LOG.warning("queue request failed")
            result, status = {"error": "internal service error"}, 500
        try:
            try:
                payload = dumps(result).encode()
            except (ValueError, TypeError, OverflowError, RecursionError):
                _LOG.warning("queue response serialization failed")
                payload, status = b'{"error":"internal service error"}', 500
            if len(payload) > MAX_BODY:
                payload, status = b'{"error":"response exceeds local limit"}', 503
            self._live()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass


def server(
    host, port, service, token, *, concurrency=32, request_timeout=10, maintenance=None
):
    return _Server(
        (host, port),
        service,
        token,
        concurrency=concurrency,
        request_timeout=request_timeout,
        maintenance=maintenance,
    )
