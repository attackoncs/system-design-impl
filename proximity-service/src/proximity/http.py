import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import threading
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from .store import Error, canonical

MAX_BODY = 65536
MAX_SNAPSHOT = 128 * 1024 * 1024


def fetch(url, token=None, timeout=3):
    headers = {"Authorization": "Bearer " + token} if token else {}
    with urlopen(Request(url, headers=headers), timeout=timeout) as response:
        data = response.read(MAX_SNAPSHOT + 1)
        if len(data) > MAX_SNAPSHOT:
            raise ValueError("snapshot exceeds local 128 MiB limit")
        return json.loads(data)


class Replicator:
    def __init__(self, store, upstream, token, interval=1):
        self.store, self.upstream, self.token, self.interval = (
            store,
            upstream.rstrip("/"),
            token,
            interval,
        )
        self.stop = threading.Event()
        self.last_error = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.is_set():
            try:
                self.store.import_snapshot(
                    fetch(self.upstream + "/internal/snapshot", self.token)
                )
                self.last_error = None
            except (
                OSError,
                ValueError,
                Error,
                KeyError,
                TypeError,
                AttributeError,
                sqlite3.Error,
            ):
                self.last_error = "snapshot synchronization failed"
            self.stop.wait(self.interval)


class Router:
    def __init__(self, regions):
        if (
            not isinstance(regions, dict)
            or not regions
            or any(not isinstance(v, list) or not v for v in regions.values())
        ):
            raise ValueError("regions require nonempty replica URL lists")
        for urls in regions.values():
            for url in urls:
                parsed = urlsplit(url)
                if (
                    parsed.scheme not in ("http", "https")
                    or not parsed.netloc
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError("invalid replica URL")
        self.regions = regions
        self.counter = 0
        self.lock = threading.Lock()

    def read(self, region, path):
        if region not in self.regions:
            raise Error(400, "unknown region; X-Region header required")
        urls = self.regions[region]
        with self.lock:
            start = self.counter % len(urls)
            self.counter += 1
        for i in range(len(urls)):
            try:
                return fetch(
                    urls[(start + i) % len(urls)].rstrip("/") + path, timeout=1
                )
            except HTTPError as exc:
                if exc.code < 500:
                    raise Error(exc.code, "replica rejected request")
            except (URLError, OSError, ValueError):
                pass
        raise Error(503, "no available replica in requested region")


def server(host, port, store=None, tokens=None, router=None, replicator=None):
    tokens = tokens or {}
    if set(tokens) - {"owners", "admin", "replication"}:
        raise ValueError("unknown token configuration fields")
    owner_tokens = tokens.get("owners", {})
    if not isinstance(owner_tokens, dict) or any(
        not isinstance(t, str) or not t or not isinstance(o, str) or not o
        for t, o in owner_tokens.items()
    ):
        raise ValueError("owners must map nonempty tokens to owner IDs")

    class Handler(BaseHTTPRequestHandler):
        # Apply before parsing headers, including clients that send no request.
        timeout = 10.0

        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # Never access-log precise query coordinates or tokens.

        def auth(self, kind):
            value = self.headers.get("Authorization", "")
            if not value.startswith("Bearer "):
                raise Error(401, "Bearer token required")
            value = value[7:]
            if kind == "owners":
                for token, owner in owner_tokens.items():
                    if hmac.compare_digest(value, token):
                        return owner
            elif tokens.get(kind) and hmac.compare_digest(value, tokens[kind]):
                return kind
            raise Error(403, "invalid credential")

        def body(self):
            if self.headers.get("Transfer-Encoding"):
                raise Error(400, "Transfer-Encoding unsupported")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= MAX_BODY:
                raise Error(413, "body exceeds 64 KiB")
            return json.loads(self.rfile.read(size))

        def execute(self):
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query, keep_blank_values=True)
            if any(len(v) != 1 for v in query.values()):
                raise Error(400, "duplicate query parameter")
            params = {k: v[0] for k, v in query.items()}
            if self.command == "GET" and path == "/health":
                if router:
                    return {"role": "gateway", "regions": list(router.regions)}
                result = store.health()
                if replicator:
                    result["replication_error"] = replicator.last_error
                return result
            if router:
                if self.command != "GET" or not (
                    path == "/v1/search/nearby" or path.startswith("/v1/businesses/")
                ):
                    raise Error(403, "gateway supports public reads only")
                return router.read(self.headers.get("X-Region"), self.path)
            if path == "/internal/snapshot" and self.command == "GET":
                self.auth("replication")
                return store.export()
            if path == "/internal/publish" and self.command == "POST":
                self.auth("admin")
                if store.readonly:
                    raise Error(403, "replica is read only")
                return store.publish(force=True)
            if path == "/v1/search/nearby" and self.command == "GET":
                if set(params) - {
                    "latitude",
                    "longitude",
                    "radius",
                    "category",
                    "open_at",
                    "limit",
                    "cursor",
                }:
                    raise ValueError("unknown search parameter")
                return store.search(
                    params["latitude"],
                    params["longitude"],
                    int(params.get("radius", "5000")),
                    params.get("category"),
                    params.get("open_at"),
                    int(params.get("limit", "100")),
                    params.get("cursor"),
                )
            if path.startswith("/v1/businesses/"):
                identifier = path[len("/v1/businesses/") :]
                if self.command == "GET":
                    return store.detail(identifier)
                if self.command in ("PUT", "DELETE"):
                    owner = self.auth("owners")
                    body = self.body() if self.command == "PUT" else None
                    return store.mutate(
                        owner,
                        self.command,
                        identifier,
                        body,
                        self.headers.get("Idempotency-Key"),
                    )
            if path == "/v1/businesses" and self.command == "POST":
                owner = self.auth("owners")
                return store.mutate(
                    owner,
                    "POST",
                    None,
                    self.body(),
                    self.headers.get("Idempotency-Key"),
                )
            raise Error(404, "route not found")

        def handle_request(self):
            self.connection.settimeout(10)
            try:
                result, status = self.execute(), 200
            except Error as exc:
                result, status = {"error": exc.message}, exc.status
            except (ValueError, TypeError, KeyError, OverflowError):
                result, status = {"error": "invalid request"}, 400
            except Exception:
                result, status = {"error": "internal service error"}, 500
            payload = canonical(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(payload)
            except (OSError, TimeoutError):
                pass

        do_GET = do_POST = do_PUT = do_DELETE = handle_request

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd
