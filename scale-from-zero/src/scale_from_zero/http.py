"""HTTP adapter: one request handler per lab role over strict JSON framing.

The handler parses, authenticates and dispatches; domain rules stay in App,
Store, Cache, Consumer, LoadBalancer and Fence.
"""

import hmac
from http.server import BaseHTTPRequestHandler
from contextlib import nullcontext
import sqlite3
import threading

from .assets import ASSETS
from .balancer import LoadBalancer
from .cache import Cache
from .common import Error, encode, identifier, integer, request
from .contracts import freeze, fields
from .routes import target
from .routing import partition
from .transport import loads, length
from .server_runtime import Server

# Body fields of the per-user database RPCs shared by primaries and replicas.
USER_OPERATIONS = {
    "/internal/get": {"user"},
    "/internal/update": {"user", "profile"},
    "/internal/session_create": {"user"},
    "/internal/session_check": {"user", "token"},
}


def server(
    role, store, config, index, host, port, *, concurrency=32, request_timeout=15
):
    config = freeze(config)
    gate = threading.Lock()
    stats = {"requests": 0}
    balancer = LoadBalancer(config["webs"]) if role == "lb" else None
    edge_cache = Cache(ttl=30) if role == "edge" else None

    class Handler(BaseHTTPRequestHandler):
        # Apply before parsing headers, including clients that send no request.
        timeout = 10.0

        def log_message(self, *args):
            pass

        def authorization(self, required=True):
            values = self.headers.get_all("Authorization", [])
            if (
                len(values) > 1
                or required
                and len(values) != 1
                or values
                and not values[0].isascii()
            ):
                raise Error(401, "one ASCII credential required")
            return values[0] if values else ""

        def auth(self, kind):
            if not hmac.compare_digest(
                self.authorization(), "Bearer " + config[kind + "_token"]
            ):
                raise Error(401, "role credential required")

        def body(self):
            if hasattr(self, "parsed_body"):
                return self.parsed_body
            sizes = self.headers.get_all("Content-Length", [])
            if len(sizes) != 1 or self.headers.get_all("Transfer-Encoding", []):
                raise ValueError(
                    "one Content-Length without Transfer-Encoding required"
                )
            size = length(sizes[0])
            if not 0 < size <= 65536:
                raise Error(413, "body requires 1..65536 bytes")
            types = self.headers.get_all("Content-Type", [])
            if (
                len(types) != 1
                or types[0].split(";", 1)[0].strip().lower() != "application/json"
            ):
                raise ValueError("application/json required")
            raw = self.rfile.read(size)
            value = loads(raw)
            if len(raw) != size or not isinstance(value, dict):
                raise ValueError("complete JSON object required")
            self.parsed_body = value
            return value

        def dispatch(self):
            path, query = target(self.requestline.split()[1], role, self.command)
            method = self.command
            sizes = self.headers.get_all("Content-Length", [])
            if (
                len(sizes) > 1
                or method == "GET"
                and (
                    self.headers.get_all("Transfer-Encoding", [])
                    or any(length(size) != 0 for size in sizes)
                )
            ):
                raise ValueError("ambiguous or unused request body")
            self.authorization(False)
            with gate:
                stats["requests"] += 1
            if path == "/health" and method == "GET":
                return {"role": role, "index": index, "single_host_lab": True}
            if path == "/admin/status" and method == "GET":
                self.auth("admin")
                return self.admin_status()
            if role == "lb" and path == "/admin/backends":
                self.auth("admin")
                return self.backends(method)
            if role == "edge" and path == "/admin/invalidate":
                self.auth("admin")
                asset = fields(self.body(), {"asset"})["asset"]
                if asset not in ASSETS:
                    raise ValueError("known versioned asset required")
                edge_cache.delete(asset)
                return {"invalidated": asset}
            if role == "worker" and path == "/admin/control" and method == "POST":
                self.auth("admin")
                body = self.body()
                if set(body) != {"lose_ack"}:
                    raise ValueError("lose_ack required")
                return store.control(body["lose_ack"])
            if path.startswith("/internal/"):
                self.auth("internal")
                if role in ("primary", "replica"):
                    return self.database_internal(path, method, query)
                if role == "cache" and method == "POST":
                    return self.cache_internal(path)
                raise Error(404, "internal route missing")
            if role == "lb":
                return self.forward(path, method)
            if role == "edge" and path.startswith("/assets/") and method == "GET":
                return self.edge_asset(path)
            if role in ("web", "single"):
                return self.web(path, method, query)
            raise Error(404, "route missing")

        def admin_status(self):
            if role in ("primary", "replica", "worker", "cache"):
                return store.status()
            if role in ("web", "single"):
                return {
                    "web_node": store.node,
                    "cache_errors": store.cache_errors,
                    **(store.store.status() if role == "single" else {}),
                }
            if role == "edge":
                return edge_cache.status()
            with gate:
                return {"requests": stats["requests"], "failovers": balancer.failovers}

        def backends(self, method):
            if method == "GET":
                return balancer.status()
            nodes = fields(self.body(), {"nodes"})["nodes"]
            available = {
                name: url
                for name, url in config["services"].items()
                if name.startswith("web")
            }
            if (
                not isinstance(nodes, list)
                or not 1 <= len(nodes) <= 8
                or any(
                    not isinstance(node, str) or node not in available for node in nodes
                )
                or len(set(nodes)) != len(nodes)
            ):
                raise ValueError("1..8 distinct configured Web node names required")
            return balancer.configure([available[node] for node in nodes])

        def database_internal(self, path, method, query):
            if path == "/internal/changes" and method == "GET":
                return store.changes(query.get("after", 0))
            if method != "POST":
                raise Error(404, "internal route missing")
            body = self.body()
            if role == "replica" and path == "/internal/promote":
                fields(body, set())
                fence = getattr(store, "fence", None)
                if fence is None:
                    raise Error(409, "promotion fence is not configured")
                return fence.promote(store.status()["seq"])
            if path in USER_OPERATIONS:
                return self.user_operation(path, body)
            if role == "primary" and path == "/internal/claim":
                fields(body, set())
                return {"job": store.claim(lease_seconds=5)}
            if role == "primary" and path == "/internal/ack":
                fields(body, {"job"})
                return store.ack(body["job"])
            raise Error(404, "internal route missing")

        def user_operation(self, path, body):
            fields(body, USER_OPERATIONS[path])
            user = identifier(body["user"])
            if user not in config["users"]:
                raise Error(404, "configured user missing")
            if partition(user, len(config["primaries"])) != index:
                raise Error(409, "wrong database shard")
            if path == "/internal/get":
                return store.get(user)
            fence = getattr(store, "fence", None)
            if role != "primary" and not (
                path == "/internal/update" and fence is not None and fence.holds()
            ):
                raise Error(403, "replica is read-only")
            if path == "/internal/update":
                return store.update(user, body["profile"])
            if path == "/internal/session_create":
                return store.session_create(user)
            return store.session_check(user, body["token"])

        def cache_internal(self, path):
            body = self.body()
            key = identifier(body["key"])
            if path == "/internal/get":
                fields(body, {"key", "min_version"})
                return store.probe(key, integer(body["min_version"]))
            if path == "/internal/put":
                fields(body, {"key", "value", "generation"})
                return {"stored": store.put(key, body["value"], body["generation"])}
            if path == "/internal/delete":
                fields(body, {"key", "min_version"})
                store.delete(key, integer(body["min_version"]))
                return {"deleted": True}
            raise Error(404, "internal route missing")

        def forward(self, path, method):
            if path not in ("/v1/login", "/v1/profile") and not path.startswith(
                "/assets/"
            ):
                raise Error(404, "load balancer route missing")
            body = self.body() if method == "POST" else None
            token = self.authorization(False)
            return balancer.forward(
                method,
                self.path,
                token[7:] if token.startswith("Bearer ") else "",
                body,
                deadline=self.server.local.deadline,
                cancel=self.server.local.cancel,
            )

        def edge_asset(self, path):
            key = path.rsplit("/", 1)[1]
            if key not in ASSETS:
                raise Error(404, "unknown versioned asset")
            probe = edge_cache.probe(key)
            value = probe["value"]
            if value is None:
                value = request(
                    config["entry"] + path,
                    "",
                    timeout=1.5,
                    deadline=self.server.local.deadline,
                    cancel=self.server.local.cancel,
                )
                if value != {"asset": key, "content": ASSETS[key]}:
                    raise Error(502, "origin returned another immutable asset")
                edge_cache.put(key, value, probe["generation"])
            return {**value, "edge_model": "local cache; not a real CDN"}

        def web(self, path, method, query):
            if path.startswith("/assets/") and method == "GET":
                key = path.rsplit("/", 1)[1]
                if key not in ASSETS:
                    raise Error(404, "asset missing")
                return {"asset": key, "content": ASSETS[key]}
            if path == "/v1/login" and method == "POST":
                return store.login(self.body())
            if path == "/v1/profile":
                user = store.authorize(self.authorization())
                if method == "POST":
                    return store.update(user, self.body())
                if method == "GET":
                    return store.get(
                        user,
                        query.get("consistency", "eventual") == "strong",
                        query.get("min_version", 0),
                    )
            raise Error(404, "route missing")

        def handle_request(self):
            self.close_connection = True
            self.connection.settimeout(min(10, request_timeout))
            try:
                context = (
                    store.operation(
                        self.server.local.deadline, self.server.local.cancel
                    )
                    if role in {"web", "single"}
                    else nullcontext()
                )
                with context:
                    value, status = self.dispatch(), 200
            except Error as exc:
                value, status = {"error": str(exc)}, exc.status
            except (ValueError, KeyError, TypeError, RecursionError) as exc:
                value, status = {"error": str(exc)}, 400
            except (OSError, TimeoutError, sqlite3.OperationalError):
                value, status = (
                    {
                        "error": "upstream unavailable; retry original request_id for writes"
                    },
                    503,
                )
            except Exception:
                value, status = {"error": "internal failure"}, 500
            raw = encode(value).encode()
            if len(raw) > 8 * 1024 * 1024:
                status, raw = 503, b'{"error":"response exceeds 8 MiB"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = handle_request
        do_POST = handle_request

    return Server(
        (host, port), Handler, concurrency=concurrency, request_timeout=request_timeout
    )
