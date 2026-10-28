"""Bounded map APIs, region-confined read failover and owned catalog polling."""

from contextlib import suppress
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import socket
import sqlite3
import sys
import threading
import time
from types import MappingProxyType
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from .navigation import route
from .render import render_tile, routing_tile, tile_urls
from .store import Error, dumps
from .transport import (
    MAX_BODY,
    MAX_CATALOG,
    MAX_PUBLIC,
    _Deadline,
    _seconds,
    _secret,
    _url,
    integer,
    loads,
    fetch as fetch,
)


class Replicator:
    """Retain the last complete catalog and drain accepted transfer/install work."""

    def __init__(self, store, upstream, token, interval=1):
        _url(upstream)
        _seconds(interval)
        if not _secret(token):
            raise ValueError("invalid replication credential")
        self.store, self.upstream, self.token, self.interval = (
            store,
            upstream.rstrip("/"),
            token,
            interval,
        )
        self.stop = threading.Event()
        self.error = None
        self._running = threading.Lock()
        self.thread = threading.Thread(target=self.run, name="maps-replication")

    def start(self):
        if self.stop.is_set():
            raise RuntimeError("replicator is stopped")
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if self.thread is threading.current_thread():
            raise RuntimeError("replicator cannot join itself")
        if self.thread.ident is not None:
            self.thread.join()

    def run(self):
        if not self._running.acquire(False):
            raise RuntimeError("replicator already running")
        try:
            while not self.stop.is_set():
                try:
                    payload, content_type, _ = fetch(
                        self.upstream + "/internal/catalog",
                        token=self.token,
                        cancel=self.stop,
                    )
                    if (
                        content_type.split(";", 1)[0].strip().lower()
                        != "application/json"
                    ):
                        raise ValueError("invalid catalog media type")
                    snapshot = loads(payload)
                    if self.stop.is_set():
                        return
                    self.store.install_snapshot(snapshot)
                    self.error = None
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
                        self.error = "catalog synchronization failed"
                self.stop.wait(self.interval)
        finally:
            self._running.release()


def _target(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 8192
        or re.search(r"%(?![0-9a-fA-F]{2})", value)
    ):
        raise ValueError("invalid request target")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ValueError("origin-form target required")
    params = parse_qs(
        parsed.query,
        keep_blank_values=True,
        max_num_fields=16,
        strict_parsing=True,
        errors="strict",
    )
    if any(len(items) != 1 for items in params.values()):
        raise ValueError("duplicate query parameter")
    return parsed.path, {key: items[0] for key, items in params.items()}


def _public(path, method):
    if method == "POST":
        return path == "/v1/route"
    return method == "GET" and (
        path in ("/v1/geocode", "/v1/reverse", "/v1/tile-urls")
        or path.startswith(("/v1/tiles/", "/v1/routing-tiles/"))
    )


def _validate_public(payload, content_type, path):
    media = content_type.split(";", 1)[0].strip().lower()
    if path.startswith("/v1/tiles/") and path.endswith(".png"):
        if media != "image/png" or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("invalid map image response")
    elif media not in ("application/json", "application/geo+json") or not isinstance(
        loads(payload), dict
    ):
        raise ValueError("invalid public JSON response")


class Gateway:
    """Read replicas within one region, with finite admission and total budget."""

    def __init__(self, regions, *, timeout=1, request_timeout=3, concurrency=32):
        _seconds(timeout)
        _seconds(request_timeout)
        if type(concurrency) is not int or not 1 <= concurrency <= 256:
            raise ValueError("invalid gateway capacity")
        if (
            not isinstance(regions, dict)
            or not 1 <= len(regions) <= 256
            or any(
                not isinstance(name, str)
                or not 1 <= len(name) <= 100
                or not isinstance(peers, list)
                or not 1 <= len(peers) <= 8
                for name, peers in regions.items()
            )
        ):
            raise ValueError("bounded nonempty region map required")
        for peers in regions.values():
            for peer in peers:
                _url(peer)
        self.regions = MappingProxyType(
            {name: tuple(peers) for name, peers in regions.items()}
        )
        self.counter, self.lock = {}, threading.Lock()
        self.timeout, self.request_timeout = timeout, request_timeout
        self.slots = threading.BoundedSemaphore(concurrency)

    def read(self, region, path, method, body, *, cancel=None):
        if not isinstance(region, str) or region not in self.regions:
            raise Error(400, "known X-Region required")
        target, _ = _target(path)
        if not _public(target, method):
            raise Error(403, "gateway only forwards public reads")
        deadline = time.monotonic() + self.request_timeout
        if not self.slots.acquire(timeout=self.request_timeout):
            raise Error(503, "regional read capacity exhausted")
        try:
            peers = self.regions[region]
            with self.lock:
                start = self.counter.get(region, 0) % len(peers)
                self.counter[region] = start + 1
            for index in range(len(peers)):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or (cancel is not None and cancel.is_set()):
                    break
                try:
                    payload, content_type, _cache = fetch(
                        peers[(start + index) % len(peers)].rstrip("/") + path,
                        method,
                        body,
                        timeout=min(self.timeout, remaining / (len(peers) - index)),
                        deadline=deadline,
                        cancel=cancel,
                        max_bytes=MAX_PUBLIC,
                    )
                    _validate_public(payload, content_type, target)
                    if time.monotonic() >= deadline or (
                        cancel is not None and cancel.is_set()
                    ):
                        raise TimeoutError("regional read expired")
                    immutable = target.startswith(("/v1/tiles/", "/v1/routing-tiles/"))
                    return (
                        payload,
                        content_type,
                        "public, max-age=31536000, immutable"
                        if immutable
                        else "no-store",
                    )
                except HTTPError as error:
                    if 400 <= error.code < 500:
                        raise Error(
                            error.code, "regional peer rejected request"
                        ) from None
                except (OSError, ValueError, http.client.HTTPException, RecursionError):
                    pass
            raise Error(503, "no peer available in requested region")
        finally:
            self.slots.release()


class _Server(ThreadingHTTPServer):
    daemon_threads = False
    request_queue_size = 128

    def __init__(self, *args, concurrency=32, request_timeout=10):
        if type(concurrency) is not int or not 1 <= concurrency <= 256:
            raise ValueError("invalid HTTP capacity")
        self.request_timeout = _seconds(request_timeout)
        self.slots = threading.BoundedSemaphore(concurrency)
        self.events = threading.BoundedSemaphore(concurrency * 3 // 4)
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
        self.event_slot = False

    def log_message(self, *_args):
        pass

    def _live(self):
        if self.cancelled.is_set() or time.monotonic() >= self.deadline:
            raise TimeoutError("request expired")
        self.connection.settimeout(max(0.001, min(5, self.deadline - time.monotonic())))

    def header(self, name):
        values = self.headers.get_all(name, [])
        if len(values) > 1:
            raise ValueError("duplicate header")
        return values[0] if values else None

    def length(self):
        value = self.header("Content-Length")
        return None if value is None else integer(value)

    def auth(self, admin=False):
        header = self.header("Authorization")
        if not isinstance(header, str) or not header.startswith("Bearer "):
            raise Error(401, "Bearer required")
        if not _secret(header[7:]):
            raise Error(401, "invalid credential")
        return self.server.store.authenticate(header[7:], admin)

    def body(self):
        size = self.length()
        if size is None or not 0 < size <= MAX_BODY:
            raise Error(413, "request body must be 1 byte..2 MiB")
        content_type = self.header("Content-Type")
        if (
            content_type
            and content_type.split(";", 1)[0].strip().lower() != "application/json"
        ):
            raise Error(415, "JSON content type required")
        self._live()
        data = self.rfile.read(size)
        self._live()
        if len(data) != size:
            raise ValueError("truncated request body")
        result = loads(data)
        if not isinstance(result, dict):
            raise ValueError("JSON object required")
        return result

    def dispatch(self):
        if self.header("Transfer-Encoding") is not None:
            raise ValueError("transfer encoding unsupported")
        length = self.length()
        self.header("Content-Type")
        self.header("If-None-Match")
        path, params = _target(self.path)
        store, gateway = self.server.store, self.server.gateway
        if self.command in ("GET", "DELETE") and length not in (None, 0):
            raise ValueError("unexpected body")
        if path == "/health" and self.command == "GET":
            if params:
                raise ValueError("unknown health parameter")
            return (
                {"role": "gateway", "regions": list(gateway.regions)}
                if gateway
                else {
                    **store.health(),
                    "replication_error": self.server.replicator.error
                    if self.server.replicator
                    else None,
                }
            )
        if gateway:
            if not _public(path, self.command):
                raise Error(403, "gateway only forwards public map reads")
            body = self.body() if self.command == "POST" else None
            return gateway.read(
                self.header("X-Region"),
                self.path,
                self.command,
                body,
                cancel=self.cancelled,
            )
        if path == "/v1/navigation" or path.startswith("/v1/navigation/"):
            tracker = self.server.tracker
            if tracker is None or store.readonly:
                raise Error(403, "navigation sessions require primary")
            actor = self.auth()
            event_match = re.fullmatch(
                r"/v1/navigation/([A-Za-z0-9_-]{1,64})/events", path
            )
            if event_match is not None and self.command == "GET":
                if set(params) - {"after"}:
                    raise ValueError("unknown event parameter")
                header = self.header("Last-Event-ID")

                def cursor(value):
                    if (
                        not isinstance(value, str)
                        or not re.fullmatch(r"[0-9]{1,19}", value)
                        or int(value) >= 2**63
                    ):
                        raise ValueError("invalid event cursor")
                    return int(value)

                after = cursor(
                    params.get("after", header if header is not None else "0")
                )
                if header is not None and after != cursor(header):
                    raise ValueError("conflicting event cursors")
                if not self.server.events.acquire(False):
                    raise Error(503, "navigation event capacity exhausted")
                self.event_slot = True
                self._live()
                events = tracker.events(
                    actor,
                    event_match[1],
                    after=after,
                    wait=min(5, max(0, self.deadline - time.monotonic() - 0.05)),
                    cancel=self.cancelled,
                )
                payload = (
                    "".join(
                        "id: "
                        + str(event["id"])
                        + "\nevent: route\ndata: "
                        + dumps(event["data"])
                        + "\n\n"
                        for event in events
                    )
                    if events
                    else ": keep-alive\n\n"
                )
                return payload.encode(), "text/event-stream; charset=utf-8", "no-store"
            if params:
                raise ValueError("unknown navigation parameter")
            if path == "/v1/navigation" and self.command == "POST":
                return tracker.create(actor, self.body())
            matched = re.fullmatch(
                r"/v1/navigation/([A-Za-z0-9_-]{1,64})(/position)?", path
            )
            if matched is not None:
                identifier, suffix = matched.groups()
                if suffix == "/position" and self.command == "POST":
                    body = self.body()
                    if set(body) != {"origin"}:
                        raise ValueError("navigation origin required")
                    return tracker.move(actor, identifier, body["origin"])
                if suffix is None and self.command == "GET":
                    return tracker.read(actor, identifier)
                if suffix is None and self.command == "DELETE":
                    return tracker.remove(actor, identifier)
            raise Error(404, "navigation route not found")
        if path == "/internal/catalog" and self.command == "GET":
            if params:
                raise ValueError("unknown catalog parameter")
            self.auth(admin=True)
            return store.export()
        if path == "/v1/route" and self.command == "POST":
            if params:
                raise ValueError("unknown route parameter")
            body = self.body()
            if set(body) - {
                "origin",
                "destination",
                "mode",
                "avoid_tolls",
                "alternatives",
                "max_nodes",
            }:
                raise ValueError("unknown route field")
            return route(store, **body)
        if path == "/v1/geocode" and self.command == "GET":
            if set(params) != {"address"}:
                raise ValueError("address required")
            return store.geocode(params["address"])
        if path == "/v1/reverse" and self.command == "GET":
            if set(params) != {"latitude", "longitude"}:
                raise ValueError("coordinates required")
            return store.geocode(
                {
                    "latitude": float(params["latitude"]),
                    "longitude": float(params["longitude"]),
                },
                reverse=True,
            )
        if path == "/v1/tile-urls" and self.command == "GET":
            if not {"latitude", "longitude", "zoom"} <= set(params) or set(params) - {
                "latitude",
                "longitude",
                "zoom",
                "format",
            }:
                raise ValueError("coordinates and zoom required")
            return tile_urls(
                store,
                float(params["latitude"]),
                float(params["longitude"]),
                integer(params["zoom"]),
                params.get("format", "png"),
            )
        if self.command == "GET" and path.startswith("/v1/tiles/"):
            if params:
                raise ValueError("unknown tile parameter")
            graph, z, x, name = path[len("/v1/tiles/") :].split("/")
            y, format = name.split(".")
            payload, content_type = render_tile(
                store, graph, integer(z), int(x), integer(y), format
            )
            return payload, content_type, "public, max-age=31536000, immutable"
        if self.command == "GET" and path.startswith("/v1/routing-tiles/"):
            if params:
                raise ValueError("unknown routing tile parameter")
            parts = path[len("/v1/routing-tiles/") :].split("/")
            if len(parts) == 3:
                graph, x, y = parts
                z = 16
            else:
                graph, raw_z, x, y = parts
                z = integer(raw_z)
            if z not in (8, 12, 16):
                raise ValueError("unsupported routing level")
            return (
                dumps(routing_tile(store, graph, int(x), integer(y), z=z)).encode(),
                "application/json",
                "public, max-age=31536000, immutable",
            )
        if path == "/v1/locations":
            user = self.auth()
            if params:
                raise ValueError("unknown locations parameter")
            if self.command == "GET":
                return store.history(user)
            if self.command == "POST":
                body = self.body()
                if set(body) != {"reports"}:
                    raise ValueError("reports required")
                return store.ingest(user, body["reports"])
        if self.command == "POST" and path in (
            "/internal/traffic",
            "/internal/aggregate",
            "/internal/graph",
        ):
            self.auth(
                admin=True
            )  # Reject unauthorized clients before receiving their body.
            if params:
                raise ValueError("unknown internal parameter")
            body = self.body()
            if path == "/internal/traffic":
                if set(body) - {"records", "ttl"}:
                    raise ValueError("unknown traffic field")
                return store.traffic(**body)
            if path == "/internal/aggregate":
                if set(body) - {"min_users"}:
                    raise ValueError("unknown aggregate field")
                return store.aggregate(**body)
            return store.import_graph(body)
        raise Error(404, "route not found")

    def respond(self, status, result):
        self._live()
        payload, content_type, cache = (
            result
            if isinstance(result, tuple)
            else (dumps(result).encode(), "application/json", "no-store")
        )
        maximum = MAX_CATALOG if self.path == "/internal/catalog" else MAX_PUBLIC
        if len(payload) > maximum:
            raise Error(413, "response exceeds local limit")
        etag = '"' + hashlib.sha256(payload).hexdigest() + '"'
        conditional = self.header("If-None-Match")
        if (
            status == 200
            and self.command == "GET"
            and cache != "no-store"
            and conditional is not None
        ):
            matches = [value.strip() for value in conditional.split(",")]
            if "*" in matches or any(
                value.removeprefix("W/") == etag for value in matches
            ):
                status = 304
        self.response_started = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if status != 304:
            self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache)
        self.send_header("ETag", etag)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if status != 304:
            self.wfile.write(payload)

    def _handle_request(self):
        try:
            result, status = self.dispatch(), 200
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
        except Error as error:
            if not self.response_started:
                with suppress(OSError):
                    self.respond(error.status, {"error": error.message})
        except OSError:
            pass

    def handle_request(self):
        try:
            self._handle_request()
        finally:
            if self.event_slot:
                self.server.events.release()
                self.event_slot = False

    do_GET = do_POST = do_DELETE = handle_request


def server(
    host,
    port,
    store=None,
    replicator=None,
    gateway=None,
    tracker=None,
    *,
    concurrency=32,
    request_timeout=10,
):
    """Bind without starting threads; close cancels and drains accepted handlers."""
    if (store is None) == (gateway is None):
        raise ValueError("configure either store or gateway")
    httpd = _Server(
        (host, port), _Handler, concurrency=concurrency, request_timeout=request_timeout
    )
    httpd.store, httpd.replicator, httpd.gateway, httpd.tracker = (
        store,
        replicator,
        gateway,
        tracker,
    )
    return httpd
