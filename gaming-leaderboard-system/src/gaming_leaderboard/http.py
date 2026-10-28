"""Authenticated role routes with bounded request ownership."""

from http.server import BaseHTTPRequestHandler
import hmac
import sqlite3
from urllib.error import HTTPError, URLError

from .common import Error, dumps, http_json, integer
from .redis import RedisError
from .routes import contract, decimal, target
from .server_runtime import Server
from .transport import MAX_REQUEST, length, loads


def server(role, config, store, host, port, *, concurrency=32, request_timeout=15):
    if role not in ("authority", "projection"):
        raise ValueError("invalid HTTP role")
    config = loads(dumps(config))

    class Handler(BaseHTTPRequestHandler):
        timeout = 10.0

        def log_message(self, *args):
            pass

        def auth(self, kind):
            supplied = self.headers.get("Authorization", "")
            if (
                len(self.headers.get_all("Authorization", [])) != 1
                or not supplied.isascii()
                or not hmac.compare_digest(
                    supplied, "Bearer " + config[kind + "_token"]
                )
            ):
                raise Error(401, "one valid role credential required")

        def body(self):
            sizes = self.headers.get_all("Content-Length", [])
            if len(sizes) != 1 or self.headers.get_all("Transfer-Encoding", []):
                raise ValueError(
                    "one Content-Length without Transfer-Encoding required"
                )
            size = length(sizes[0])
            if not 0 < size <= MAX_REQUEST:
                raise Error(413, "JSON request requires 1..1048576 bytes")
            types = self.headers.get_all("Content-Type", [])
            if (
                len(types) != 1
                or types[0].split(";", 1)[0].strip().lower() != "application/json"
            ):
                raise ValueError("application/json required")
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError("truncated JSON body")
            value = loads(raw)
            if not isinstance(value, dict):
                raise ValueError("JSON object required")
            return value

        def dispatch(self):
            path, params = target(self.requestline.split()[1])
            method = self.command
            kind = contract(role, path, method, params)
            if len(self.headers.get_all("Content-Length", [])) > 1:
                raise ValueError("duplicate Content-Length")
            if method not in ("POST", "PUT") and (
                self.headers.get_all("Transfer-Encoding", [])
                or any(
                    length(v) != 0 for v in self.headers.get_all("Content-Length", [])
                )
            ):
                raise ValueError("body unsupported for this method")
            if kind is None:
                return {"role": role}
            self.auth(kind)
            if path in ("/admin/rebuild", "/admin/reconcile", "/admin/close"):
                self.server.set_budget(60)
            options = {
                "deadline": self.server.local.deadline,
                "cancel": self.server.local.cancel,
            }
            body = self.body() if method in ("POST", "PUT") else None
            if role == "projection":
                if path == "/admin/rebuild":
                    if body:
                        raise ValueError("rebuild body must be empty")
                    return store.reset(**options)
                if path == "/internal/status":
                    return store.status(**options)
                return store.query(body, **options)
            # Do not acquire public query capacity or locks for the log feed:
            # projection barriers need these requests to make progress.
            if path == "/internal/status":
                return {**store.journal.status(), "service": store.metrics()}
            if path == "/internal/feed":
                return store.journal.feed(
                    decimal(params["shard"], "shard"), decimal(params["after"], "after")
                )
            with store.operation(**options), store.guard(method, path):
                if path.startswith("/admin/players/"):
                    if set(body) != {"name"}:
                        raise ValueError("player requires name")
                    return store.journal.register(path.rsplit("/", 1)[1], body["name"])
                if path in ("/admin/close", "/admin/reconcile"):
                    if set(body) != {"month"}:
                        raise ValueError("operation requires month")
                    return (
                        store.journal.close(body["month"])
                        if path == "/admin/close"
                        else store.reconcile(body["month"])
                    )
                if path == "/admin/rebuild":
                    if body:
                        raise ValueError("rebuild body must be empty")
                    for url in config["projection_urls"]:
                        http_json(
                            url + "/admin/rebuild", config["admin_token"], {}, **options
                        )
                    return {
                        "projected": store.ready(store.journal.status()["heads"]),
                        "rebuild_requested": True,
                    }
                if path == "/v1/scores" and method == "POST":
                    receipt = store.journal.ingest(body)
                    projected = store.ready(store.journal.status()["heads"], timeout=1)
                    return {
                        **receipt,
                        "projected": projected,
                        "durable": True,
                    }, 200 if projected else 202
                if path == "/v1/scores":
                    return store.query(params.get("month"))
                if path.startswith("/v1/scores/"):
                    radius = decimal(params.get("neighbors", "0"), "neighbors")
                    integer(radius, "neighbors", 0, 4)
                    return store.query(
                        params.get("month"), path.rsplit("/", 1)[1], radius
                    )
                return store.season(path.rsplit("/", 1)[1])

        def handle_request(self):
            try:
                value = self.dispatch()
                value, status = value if isinstance(value, tuple) else (value, 200)
            except Error as exc:
                value, status = {"error": str(exc)}, exc.status
            except HTTPError as exc:
                try:
                    value, status = loads(exc.read(1024 * 1024)), exc.code
                    if not isinstance(value, dict):
                        value = {"error": "upstream failure"}
                except Exception:
                    value, status = {"error": "upstream failure"}, 503
                finally:
                    exc.close()
            except (ValueError, TypeError, KeyError, RecursionError) as exc:
                value, status = {"error": str(exc)}, 400
            except (
                RedisError,
                URLError,
                OSError,
                TimeoutError,
                sqlite3.OperationalError,
            ):
                value, status = {"error": "ranking unavailable or catching up"}, 503
            except Exception:
                value, status = {"error": "internal failure"}, 500
            raw = dumps(value).encode()
            self.close_connection = True
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)
            except OSError:
                pass

        do_GET = handle_request
        do_POST = handle_request
        do_PUT = handle_request
        do_DELETE = handle_request

    return Server(
        (host, port), Handler, concurrency=concurrency, request_timeout=request_timeout
    )
