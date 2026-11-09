"""Bounded HTTP shard-query server; generation selection stays in the services."""

import contextlib
import http.server
import json
import socket
import sqlite3
import threading
import urllib.parse

import search_autocomplete.store
from search_autocomplete.coordinator import Coordinator

__all__ = ["BoundedHTTPServer", "Coordinator", "server"]


class BoundedHTTPServer(http.server.ThreadingHTTPServer):
    """Threaded HTTP server with 64 request slots and bounded socket waits."""

    request_queue_size = 128
    # server_close joins handlers before the caller closes their service/store.
    daemon_threads = False

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(64)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(0.1)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                    b"Cache-Control: no-store\r\nConnection: close\r\n\r\n"
                )
                request.shutdown(socket.SHUT_WR)
                # Drain already-sent request data so Windows does not replace
                # the overload response with a connection reset on close.
                request.recv(8192)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3)
        return connection, address


def _parameters(target):
    if len(target) > 2048:
        raise ValueError("request target too long")
    parsed = urllib.parse.urlsplit(target)
    if parsed.path != "/suggest":
        return None
    params = urllib.parse.parse_qs(
        parsed.query, keep_blank_values=True, max_num_fields=8
    )
    if any(len(values) != 1 for values in params.values()) or (
        set(params) - {"prefix", "version", "revision", "shard"}
    ):
        raise ValueError("invalid parameter")
    return {name: values[0] for name, values in params.items()}


def _integer(params, name, default):
    if name not in params:
        return default
    value = params[name]
    if not value or not value.isascii() or not value.isdecimal():
        raise ValueError("invalid integer parameter")
    return int(value)


def _suggest(service, params, shard):
    prefix = params.get("prefix", "")
    if isinstance(service, Coordinator):
        if set(params) - {"prefix"}:
            raise ValueError("coordinator selects the serving generation")
        version, revision, suggestions = service.suggest(prefix)
    else:
        active, actual_revision, _ = service.store.view()
        version = _integer(params, "version", active)
        revision = _integer(params, "revision", actual_revision)
        requested = _integer(params, "shard", shard)
        if requested != shard:
            raise ValueError("wrong shard endpoint")
        suggestions = service.suggest(prefix, version, revision, shard)
    return {
        "version": version,
        "revision": revision,
        "shard": shard,
        "suggestions": [
            {"query": query, "frequency": frequency} for query, frequency in suggestions
        ],
    }


def server(service, host="127.0.0.1", port=0, shard=None):
    """Bind a server; its caller owns serve_forever, shutdown and server_close."""
    if shard is not None and (type(shard) is not int or not 0 <= shard < 256):
        raise ValueError("invalid shard")

    class Handler(http.server.BaseHTTPRequestHandler):
        """Thin request adapter; generation selection and parsing are testable."""

        timeout = 3.0

        def log_message(self, *_):
            pass

        def do_GET(self):
            status = 200
            try:
                params = _parameters(self.path)
                if params is None:
                    status, body = 404, {"error": "unknown endpoint"}
                else:
                    body = _suggest(service, params, shard)
            except (ValueError, KeyError):
                status, body = 400, {"error": "invalid request"}
            except (search_autocomplete.store.Unavailable, sqlite3.Error):
                status, body = (
                    503,
                    {"error": "required generation or shard unavailable; retry"},
                )
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            # A version-pinned browser cache would still hide new removal rules.
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            with contextlib.suppress(OSError):
                self.wfile.write(payload)

    return BoundedHTTPServer((host, port), Handler)
