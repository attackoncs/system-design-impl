"""Bounded public APIs, region failover and owned snapshot replication.

Precise search coordinates and credentials are never access-logged. Socket
budgets cannot forcibly interrupt OS DNS resolution or arbitrary metadata work.
"""

from contextlib import suppress
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import re
import socket
import sqlite3
import sys
import threading
import time
from types import MappingProxyType
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from .store import Error, canonical

MAX_BODY = 65536
MAX_SNAPSHOT = 128 * 1024 * 1024
MAX_PUBLIC = 16 * 1024 * 1024


def _seconds(value, *, zero=False):
    try:
        valid = (
            type(value) in (int, float)
            and math.isfinite(value)
            and (value >= 0 if zero else value > 0)
            and value <= threading.TIMEOUT_MAX
        )
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("invalid duration")
    return value


def _url(value, *, query=False):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 8192
        or any(ord(c) <= 32 for c in value)
    ):
        raise ValueError("invalid HTTP endpoint")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.port == 0
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.query and not query)
    ):
        raise ValueError("invalid HTTP endpoint")
    return parsed


def _secret(value):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 4096
        and value.isascii()
        and all(ord(c) > 32 for c in value)
    )


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("non-finite JSON number")


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


class _Deadline:
    """Own one short-lived deadline watcher and join it on every exit path."""

    def __init__(self, deadline, interrupt, cancel=None):
        self.deadline, self.interrupt, self.cancel = deadline, interrupt, cancel
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self._watch, name="proximity-io-deadline")

    def _watch(self):
        while not self.finished.is_set():
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or (self.cancel is not None and self.cancel.is_set()):
                self.interrupt()
                return
            self.finished.wait(min(0.05, remaining))

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.finished.set()
        self.thread.join()


def fetch(
    url, token=None, timeout=3, *, deadline=None, cancel=None, max_bytes=MAX_SNAPSHOT
):
    """Read one bounded JSON response directly, without redirects or proxies.

    Host/SNI and normal HTTPS certificate validation are retained. The deadline
    covers headers and streaming body reads; a cancelled operation keeps its
    caller's capacity slot until the underlying operation actually exits.
    """
    _seconds(timeout)
    parsed = _url(url, query=True)
    if token is not None and not _secret(token):
        raise ValueError("invalid bearer credential")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_SNAPSHOT:
        raise ValueError("invalid response limit")
    deadline = (
        min(time.monotonic() + timeout, deadline)
        if deadline is not None
        else time.monotonic() + timeout
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0 or (cancel is not None and cancel.is_set()):
        raise TimeoutError("HTTP operation expired")
    connection_type = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_type(parsed.hostname, parsed.port, timeout=remaining)
    response, state_lock = None, threading.Lock()

    def interrupt():
        with state_lock:
            active_socket = connection.sock
            active_file = response.fp if response is not None else None
            if active_file is not None:
                active_socket = active_file.raw._sock
            if active_socket is not None:
                with suppress(OSError):
                    active_socket.shutdown(socket.SHUT_RDWR)

    try:
        with _Deadline(deadline, interrupt, cancel):
            connection.connect()
            if time.monotonic() >= deadline or (cancel is not None and cancel.is_set()):
                raise TimeoutError("HTTP operation expired")
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            headers = {"Accept": "application/json"}
            if token is not None:
                headers["Authorization"] = "Bearer " + token
            connection.request("GET", target, headers=headers)
            received = connection.getresponse()
            with state_lock:
                response = received
            if response.status != 200:
                # A redirect is evidence of a different endpoint, never authority
                # to send the replication credential somewhere else.
                raise HTTPError(
                    url,
                    response.status,
                    "upstream rejected request",
                    response.headers,
                    None,
                )
            lengths = response.headers.get_all("Content-Length", [])
            encodings = response.headers.get_all("Transfer-Encoding", [])
            if (
                len(lengths) > 1
                or len(encodings) > 1
                or (lengths and encodings)
                or (encodings and encodings[0].strip().lower() != "chunked")
            ):
                raise ValueError("ambiguous upstream response framing")
            if lengths and _integer(lengths[0]) > max_bytes:
                raise ValueError("upstream response exceeds local limit")
            data = bytearray()
            while len(data) <= max_bytes:
                if time.monotonic() >= deadline or (
                    cancel is not None and cancel.is_set()
                ):
                    raise TimeoutError("HTTP operation expired")
                part = response.read1(min(65536, max_bytes + 1 - len(data)))
                if not part:
                    break
                data.extend(part)
            if len(data) > max_bytes:
                raise ValueError("upstream response exceeds local limit")
            # read1() may return EOF before Content-Length is exhausted without
            # raising IncompleteRead. A complete JSON value is not sufficient
            # evidence that its HTTP message arrived intact.
            if response.length not in (None, 0):
                raise ValueError("truncated upstream response")
            if time.monotonic() >= deadline or (cancel is not None and cancel.is_set()):
                raise TimeoutError("HTTP operation expired")
            result = json.loads(
                data,
                object_pairs_hook=_pairs,
                parse_constant=_constant,
                parse_float=_float,
            )
            if time.monotonic() >= deadline or (cancel is not None and cancel.is_set()):
                raise TimeoutError("HTTP operation expired")
            return result
    finally:
        if response is not None:
            response.close()
        connection.close()


class Replicator:
    """One owned polling thread; close cancels active I/O and joins it."""

    def __init__(self, store, upstream, token, interval=1):
        _url(upstream)
        _seconds(interval, zero=True)
        if not _secret(token):
            raise ValueError("invalid replication credential")
        self.store, self.upstream, self.token = store, upstream.rstrip("/"), token
        self.interval = interval
        self.stop = threading.Event()
        self.last_error = None
        self._running = threading.Lock()
        self.thread = threading.Thread(target=self.run, name="proximity-replication")

    def start(self):
        if self.stop.is_set():
            raise RuntimeError("replicator is stopped")
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if (
            self.thread.ident is not None
            and self.thread is not threading.current_thread()
        ):
            self.thread.join()

    def run(self):
        if not self._running.acquire(False):
            raise RuntimeError("replicator already running")
        try:
            while not self.stop.is_set():
                try:
                    snapshot = fetch(
                        self.upstream + "/internal/snapshot",
                        self.token,
                        cancel=self.stop,
                    )
                    if self.stop.is_set():
                        return
                    self.store.import_snapshot(snapshot)
                    self.last_error = None
                except (
                    OSError,
                    ValueError,
                    Error,
                    KeyError,
                    TypeError,
                    AttributeError,
                    sqlite3.Error,
                    http.client.HTTPException,
                    RecursionError,
                ):
                    if not self.stop.is_set():
                        self.last_error = "snapshot synchronization failed"
                # Zero remains useful for deterministic fault tests, without a
                # hot retry loop when a configured endpoint fails immediately.
                self.stop.wait(max(0.001, self.interval))
        finally:
            self._running.release()


class Router:
    """Region-confined reads with bounded in-flight work and one failover budget."""

    def __init__(self, regions, *, timeout=1, request_timeout=2, concurrency=32):
        _seconds(timeout)
        _seconds(request_timeout)
        if type(concurrency) is not int or not 1 <= concurrency <= 256:
            raise ValueError("invalid router capacity")
        if (
            not isinstance(regions, dict)
            or not 1 <= len(regions) <= 256
            or any(
                not isinstance(name, str)
                or not 1 <= len(name) <= 100
                or not isinstance(urls, list)
                or not 1 <= len(urls) <= 8
                for name, urls in regions.items()
            )
        ):
            raise ValueError("regions require bounded nonempty replica URL lists")
        for urls in regions.values():
            for url in urls:
                _url(url)
        self.regions = MappingProxyType(
            {name: tuple(urls) for name, urls in regions.items()}
        )
        self.timeout, self.request_timeout = timeout, request_timeout
        self.counter, self.lock = {}, threading.Lock()
        self.slots = threading.BoundedSemaphore(concurrency)

    def read(self, region, path, *, cancel=None):
        if not isinstance(region, str) or region not in self.regions:
            raise Error(400, "unknown region; X-Region header required")
        target, _params = _target(path)
        if target != "/v1/search/nearby" and not _business_id(target):
            raise Error(400, "invalid public read route")
        deadline = time.monotonic() + self.request_timeout
        if not self.slots.acquire(timeout=self.request_timeout):
            raise Error(503, "regional read capacity exhausted")
        try:
            if time.monotonic() >= deadline:
                raise Error(503, "regional read expired")
            urls = self.regions[region]
            with self.lock:
                start = self.counter.get(region, 0) % len(urls)
                self.counter[region] = start + 1
            for index in range(len(urls)):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or (cancel is not None and cancel.is_set()):
                    break
                attempt = min(self.timeout, remaining / (len(urls) - index))
                try:
                    value = fetch(
                        urls[(start + index) % len(urls)].rstrip("/") + path,
                        timeout=attempt,
                        deadline=deadline,
                        cancel=cancel,
                        max_bytes=MAX_PUBLIC,
                    )
                    if time.monotonic() >= deadline or (
                        cancel is not None and cancel.is_set()
                    ):
                        raise TimeoutError("regional read expired")
                    if not isinstance(value, dict):
                        raise ValueError("invalid public response")
                    return value
                except HTTPError as error:
                    if 400 <= error.code < 500:
                        raise Error(error.code, "replica rejected request") from None
                except (OSError, ValueError, http.client.HTTPException, RecursionError):
                    pass
            raise Error(503, "no available replica in requested region")
        finally:
            self.slots.release()


def _target(value):
    if (
        not isinstance(value, str)
        or len(value) > 8192
        or re.search(r"%(?![0-9a-fA-F]{2})", value)
    ):
        raise ValueError("invalid request target")
    target = urlsplit(value)
    if target.scheme or target.netloc or target.fragment:
        raise ValueError("origin-form request target required")
    values = parse_qs(
        target.query,
        keep_blank_values=True,
        max_num_fields=16,
        strict_parsing=True,
        errors="strict",
    )
    if any(len(items) != 1 for items in values.values()):
        raise ValueError("duplicate query parameter")
    return target.path, {name: items[0] for name, items in values.items()}


def _business_id(path):
    prefix = "/v1/businesses/"
    identifier = path[len(prefix) :] if path.startswith(prefix) else ""
    return identifier if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier) else None


def _integer(value):
    if (
        not isinstance(value, str)
        or len(value) > 10
        or re.fullmatch(r"[0-9]+", value) is None
    ):
        raise ValueError("invalid integer parameter")
    return int(value)


class _Server(ThreadingHTTPServer):
    daemon_threads = False
    request_queue_size = 128

    def __init__(self, *args, concurrency=32, request_timeout=10):
        if type(concurrency) is not int or not 1 <= concurrency <= 256:
            raise ValueError("invalid HTTP capacity")
        self.request_timeout = _seconds(request_timeout)
        self.slots = threading.BoundedSemaphore(concurrency)
        self.stopping, self._local = threading.Event(), threading.local()
        super().__init__(*args)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(min(5, self.request_timeout))
        return request, address

    def process_request(self, request, address):
        if not self.slots.acquire(False):
            try:
                request.settimeout(0.1)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                    b"Cache-Control: no-store\r\nConnection: close\r\n\r\n"
                )
                request.shutdown(socket.SHUT_WR)
                request.recv(8192)
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
        self._local.cancelled = threading.Event()

        # Capture the event explicitly: the watcher has its own thread-local state.
        cancelled = self._local.cancelled

        def cancel_socket():
            cancelled.set()
            with suppress(OSError):
                request.shutdown(socket.SHUT_RDWR)

        try:
            with _Deadline(
                time.monotonic() + self.request_timeout, cancel_socket, self.stopping
            ):
                super().process_request_thread(request, address)
        finally:
            del self._local.cancelled
            self.slots.release()

    def handle_error(self, request, address):
        cancelled = getattr(self._local, "cancelled", None)
        if (
            cancelled is not None
            and cancelled.is_set()
            and isinstance(sys.exc_info()[1], OSError)
        ):
            return
        super().handle_error(request, address)

    def server_close(self):
        self.stopping.set()
        super().server_close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.cancelled = self.server._local.cancelled
        self.deadline = time.monotonic() + self.server.request_timeout
        self.response_started = False

    def log_message(self, *_args):
        pass

    def _live(self):
        if self.cancelled.is_set() or time.monotonic() >= self.deadline:
            raise TimeoutError("socket request expired")
        self.connection.settimeout(max(0.001, min(5, self.deadline - time.monotonic())))

    def _header(self, name):
        values = self.headers.get_all(name, [])
        if len(values) > 1:
            raise ValueError("duplicate header")
        return values[0] if values else None

    def _length(self):
        value = self._header("Content-Length")
        return None if value is None else _integer(value)

    def auth(self, kind):
        value = self._header("Authorization")
        if not isinstance(value, str) or not value.startswith("Bearer "):
            raise Error(401, "Bearer token required")
        token = value[7:]
        if not _secret(token):
            raise Error(403, "invalid credential")
        digest = hashlib.sha256(token.encode()).digest()
        if kind == "owners":
            owner = self.server.owners.get(digest)
            if owner is not None:
                return owner
        elif kind in self.server.credentials and hmac.compare_digest(
            digest, self.server.credentials[kind]
        ):
            return kind
        raise Error(403, "invalid credential")

    def body(self):
        size = self._length()
        if size is None or not 0 <= size <= MAX_BODY:
            raise Error(413, "body exceeds local 64 KiB limit")
        self._live()
        data = self.rfile.read(size)
        if len(data) != size:
            raise ValueError("truncated body")
        self._live()
        value = json.loads(
            data, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float
        )
        if not isinstance(value, dict):
            raise ValueError("business object required")
        self._live()
        return value

    def search(self, params):
        if (
            not {"latitude", "longitude"} <= set(params)
            or set(params)
            - {
                "latitude",
                "longitude",
                "radius",
                "category",
                "open_at",
                "limit",
                "cursor",
            }
            or any(not value for value in params.values())
        ):
            raise ValueError("invalid search parameters")
        return self.server.store.search(
            params["latitude"],
            params["longitude"],
            _integer(params.get("radius", "5000")),
            params.get("category"),
            params.get("open_at"),
            _integer(params.get("limit", "100")),
            params.get("cursor"),
        )

    def execute(self):
        if self._header("Transfer-Encoding") is not None:
            raise ValueError("transfer encoding unsupported")
        length = self._length()
        self._header("Content-Type")
        path, params = _target(self.path)
        store, router = self.server.store, self.server.router
        if self.command in ("GET", "DELETE") and length not in (None, 0):
            raise ValueError("unexpected body")
        if self.command == "GET" and path == "/health":
            if params:
                raise ValueError("unknown health parameter")
            if router is not None:
                return {"role": "gateway", "regions": list(router.regions)}
            result = store.health()
            if self.server.replicator is not None:
                result["replication_error"] = self.server.replicator.last_error
            publisher = getattr(self.server, "publisher", None)
            if publisher is not None:
                result["publication_error"] = publisher.last_error
            return result
        if router is not None:
            if self.command != "GET" or not (
                path == "/v1/search/nearby" or _business_id(path)
            ):
                raise Error(403, "gateway supports public reads only")
            return router.read(
                self._header("X-Region"), self.path, cancel=self.cancelled
            )
        if self.command == "GET" and path == "/internal/snapshot":
            if params:
                raise ValueError("unknown snapshot parameter")
            self.auth("replication")
            return store.export()
        if self.command == "POST" and path == "/internal/publish":
            self.auth("admin")
            if params or length not in (None, 0):
                raise ValueError("publication request is bodyless")
            if store.readonly:
                raise Error(403, "replica is read only")
            return store.publish(force=True)
        if self.command == "GET" and path == "/v1/search/nearby":
            return self.search(params)
        identifier = _business_id(path)
        if identifier is not None:
            if params:
                raise ValueError("unknown business parameter")
            if self.command == "GET":
                return store.detail(identifier)
            if self.command in ("PUT", "DELETE"):
                owner = self.auth("owners")
                return store.mutate(
                    owner,
                    self.command,
                    identifier,
                    self.body() if self.command == "PUT" else None,
                    self._header("Idempotency-Key"),
                )
        if self.command == "POST" and path == "/v1/businesses":
            owner = self.auth("owners")
            if params:
                raise ValueError("unknown business parameter")
            return store.mutate(
                owner, "POST", None, self.body(), self._header("Idempotency-Key")
            )
        raise Error(404, "route not found")

    def respond(self, status, result):
        self._live()
        payload = canonical(result).encode()
        maximum = MAX_SNAPSHOT if self.path == "/internal/snapshot" else MAX_PUBLIC
        if len(payload) > maximum:
            raise OSError("response exceeds local limit")
        self.response_started = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(payload)

    def handle_request(self):
        try:
            result, status = self.execute(), 200
        except Error as error:
            result, status = {"error": error.message}, error.status
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            result, status = {"error": "invalid request"}, 400
        except (sqlite3.Error, OSError):
            result, status = {"error": "service temporarily unavailable"}, 503
        except Exception:
            result, status = {"error": "internal service error"}, 500
        try:
            self.respond(status, result)
        except OSError:
            if not self.response_started:
                with suppress(OSError):
                    self.respond(503, {"error": "response unavailable"})

    do_GET = do_POST = do_PUT = do_DELETE = handle_request


def server(
    host,
    port,
    store=None,
    tokens=None,
    router=None,
    replicator=None,
    *,
    concurrency=32,
    request_timeout=10,
):
    """Bind an unstarted server; close drains handlers and owned I/O watchers."""
    tokens = {} if tokens is None else tokens
    if not isinstance(tokens, dict) or set(tokens) - {"owners", "admin", "replication"}:
        raise ValueError("invalid token configuration")
    owners = tokens.get("owners", {})
    if not isinstance(owners, dict) or any(
        not _secret(token) or not isinstance(owner, str) or not owner
        for token, owner in owners.items()
    ):
        raise ValueError("invalid owner credentials")
    credentials = {}
    for kind in ("admin", "replication"):
        if kind in tokens:
            if not _secret(tokens[kind]):
                raise ValueError("invalid service credential")
            credentials[kind] = hashlib.sha256(tokens[kind].encode()).digest()
    if (store is None) == (router is None):
        raise ValueError("configure either store or router")
    httpd = _Server(
        (host, port), _Handler, concurrency=concurrency, request_timeout=request_timeout
    )
    httpd.store, httpd.router, httpd.replicator = store, router, replicator
    httpd.owners = {
        hashlib.sha256(token.encode()).digest(): owner
        for token, owner in owners.items()
    }
    httpd.credentials = credentials
    return httpd
