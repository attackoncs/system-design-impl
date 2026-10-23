from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from .common import Error, dumps, http_json, identifier, integer
from .redis import RedisError


def server(role, config, store, host, port):
    class Handler(BaseHTTPRequestHandler):
        # Apply before parsing headers, including clients that send no request.
        timeout = 10.0

        def log_message(self, *args):
            pass

        def auth(self, kind):
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, "Bearer " + config[kind + "_token"]):
                raise Error(401, "valid role credential required")

        def body(self):
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024 * 1024:
                raise Error(413, "JSON request requires 1..1048576 bytes")
            return json.loads(self.rfile.read(size))

        def dispatch(self):
            target = urlsplit(self.path)
            path, method = target.path, self.command
            params = parse_qs(target.query, keep_blank_values=True)
            if any(len(v) != 1 for v in params.values()):
                raise ValueError("repeated parameter")
            params = {k: v[0] for k, v in params.items()}
            if path == "/health" and method == "GET":
                return {"role": role}
            if role == "projection":
                if path == "/admin/rebuild" and method == "POST":
                    self.auth("admin")
                    return store.reset()
                self.auth("internal")
                if path == "/internal/status" and method == "GET":
                    with store.lock:
                        client = store.master()
                        return {
                            "shard": store.shard,
                            "position": store.position(client),
                            "lineage": client.command("GET", store.root + ":lineage"),
                            "worker": dict(store.state),
                        }
                if path == "/internal/query" and method == "POST":
                    return store.query(self.body())
                raise Error(404, "unknown projection route")
            if path.startswith("/internal/"):
                self.auth("internal")
                if path == "/internal/status" and method == "GET":
                    return store.journal.status()
                if path == "/internal/feed" and method == "GET":
                    if set(params) != {"shard", "after"}:
                        raise ValueError("feed requires shard,after")
                    return store.journal.feed(
                        int(params["shard"]), int(params["after"])
                    )
                raise Error(404, "unknown authority route")
            with store.lock:
                if path.startswith("/admin/"):
                    self.auth("admin")
                    body = self.body()
                    if path.startswith("/admin/players/") and method == "PUT":
                        if not isinstance(body, dict) or set(body) != {"name"}:
                            raise ValueError("player requires name")
                        return store.journal.register(
                            identifier(path.split("/")[-1]), body["name"]
                        )
                    if path == "/admin/close" and method == "POST":
                        return store.journal.close(body["month"])
                    if path == "/admin/reconcile" and method == "POST":
                        return store.reconcile(body["month"])
                    if path == "/admin/rebuild" and method == "POST":
                        for url in config["projection_urls"]:
                            http_json(url + "/admin/rebuild", config["admin_token"], {})
                        return {
                            "projected": store.ready(store.journal.status()["heads"]),
                            "rebuild_requested": True,
                        }
                if path == "/v1/scores" and method == "POST":
                    self.auth("game")
                    receipt = store.journal.ingest(self.body())
                    projected = store.ready(store.journal.status()["heads"], timeout=1)
                    return (
                        {**receipt, "projected": projected, "durable": True},
                        200 if projected else 202,
                    )
                if path.startswith("/v1/") and method == "GET":
                    self.auth("read")
                    if path == "/v1/scores":
                        if set(params) - {"month"}:
                            raise ValueError("only month parameter supported")
                        return store.query(params.get("month"))
                    if path.startswith("/v1/scores/"):
                        if set(params) - {"month", "neighbors"}:
                            raise ValueError("unknown rank parameter")
                        radius = int(params.get("neighbors", 0))
                        integer(radius, "neighbors", 0, 4)
                        return store.query(
                            params.get("month"), identifier(path.split("/")[-1]), radius
                        )
                    if path.startswith("/v1/seasons/"):
                        return store.journal.month(path.split("/")[-1])
            raise Error(404, "unknown route or method")

        def handle_request(self):
            self.connection.settimeout(10)
            try:
                value = self.dispatch()
                if isinstance(value, tuple):
                    value, status = value
                else:
                    status = 200
            except Error as exc:
                value, status = {"error": str(exc)}, exc.status
            except HTTPError as exc:
                try:
                    value, status = json.loads(exc.read(1024 * 1024)), exc.code
                except Exception:
                    value, status = {"error": "upstream failure"}, 503
            except (ValueError, TypeError, KeyError) as exc:
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
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        do_GET = handle_request
        do_POST = handle_request
        do_PUT = handle_request

    return ThreadingHTTPServer((host, port), Handler)
