from abc import ABC, abstractmethod
from contextlib import nullcontext
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
from urllib.parse import parse_qs, urlsplit

from .common import Error, encode, integer, request
from .market import candles, levels, report


class IdentityProvider(ABC):
    @abstractmethod
    def owner(self, authorization):
        pass


class StaticIdentity(IdentityProvider):
    def __init__(self, owners):
        self.owners = owners

    def owner(self, authorization):
        for owner, token in self.owners.items():
            if hmac.compare_digest(authorization, "Bearer " + token):
                return owner
        raise Error(401, "owner credential required")


def server(role, store, config, gate, host, port):
    identity = StaticIdentity(config["owners"])

    class Handler(BaseHTTPRequestHandler):
        # Apply before parsing headers, including clients that send no request.
        timeout = 10.0

        def log_message(self, *args):
            pass

        def auth(self, kind):
            supplied = self.headers.get("Authorization", "")
            if kind == "owner":
                return identity.owner(supplied)
            if not hmac.compare_digest(supplied, "Bearer " + config[kind + "_token"]):
                raise Error(401, "role credential required")

        def body(self):
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024 * 1024:
                raise Error(413, "JSON body must be 1..1048576 bytes")
            raw = self.rfile.read(size)
            value = json.loads(raw)
            if len(raw) != size or not isinstance(value, dict):
                raise ValueError("complete JSON object required")
            return value

        def active(self):
            control = store.control()
            return request(
                config["engines"][control["active"]]
                + "/internal/serve?epoch="
                + str(control["epoch"]),
                config["internal_token"],
            )

        def publish_check(self, query):
            control = request(
                config["gateway"] + "/internal/control",
                config["internal_token"],
                timeout=2,
            )
            store.fence(control["epoch"])
            if (
                control["active"] != role
                or int(query.get("epoch", ["0"])[0]) != control["epoch"]
            ):
                raise Error(409, "matching publisher fenced by active epoch")

        def dispatch(self):
            parsed = urlsplit(self.path)
            path, method = parsed.path, self.command
            query = parse_qs(parsed.query, strict_parsing=True)
            if path == "/health" and method == "GET":
                return {"role": role, "synthetic_orders_only": True}
            if path.startswith("/admin/"):
                self.auth("admin")
                if role == "gateway":
                    if path == "/admin/status" and method == "GET":
                        return store.control()
                    if path == "/admin/clock" and method == "POST":
                        return store.clock(self.body()["timestamp"])
                    if path == "/admin/promote" and method == "POST":
                        node = self.body()["node"]
                        if node not in config["engines"]:
                            raise ValueError("unknown matching node")
                        replica = request(
                            config["engines"][node] + "/internal/raw",
                            config["internal_token"],
                            timeout=2,
                        )
                        return store.promote(node, replica)
                if role in ("primary", "backup"):
                    if path == "/admin/snapshot" and method == "POST":
                        return store.snapshot()
                    if path == "/admin/audit" and method == "GET":
                        return store.audit()
                if role == "market" and path == "/admin/status" and method == "GET":
                    value = store.view()
                    return {key: value[key] for key in ("seq", "head", "resyncs")}
                raise Error(404, "admin route missing")
            if path.startswith("/internal/"):
                self.auth("internal")
                if role == "gateway":
                    if path == "/internal/control" and method == "GET":
                        return store.control()
                    if path == "/internal/events" and method == "GET":
                        after = integer(int(query.get("after", ["0"])[0]))
                        with store.connection() as db:
                            return {"events": store.rows(db)[after : after + 100]}
                if role in ("primary", "backup"):
                    if path == "/internal/raw" and method == "GET":
                        return store.raw()
                    if path == "/internal/serve" and method == "GET":
                        self.publish_check(query)
                        return store.raw()
                    if path == "/internal/feed" and method == "GET":
                        self.publish_check(query)
                        return store.feed(integer(int(query.get("after", ["0"])[0])))
                raise Error(404, "internal route missing")
            if role == "gateway":
                owner = self.auth("owner")
                if method == "POST" and path in ("/v1/orders", "/v1/cancels"):
                    return store.submit(
                        owner, "new" if path == "/v1/orders" else "cancel", self.body()
                    )
                if path.startswith("/v1/requests/") and method == "GET":
                    seq = store.lookup(owner, path.split("/")[-1])
                    value = self.active()
                    return (
                        value["state"]
                        .get("results", {})
                        .get(str(seq), {"seq": seq, "status": "PENDING"})
                    )
                if path == "/v1/account" and method == "GET":
                    value = self.active()
                    if not value["seq"]:
                        raise Error(503, "matching engine initializing")
                    return {"seq": value["seq"], **value["state"]["accounts"][owner]}
                if path.startswith("/v1/orders/") and method == "GET":
                    value = self.active()
                    order = value["state"].get("orders", {}).get(path.split("/")[-1])
                    if not order or order["owner"] != owner:
                        raise Error(404, "owned order missing")
                    return order
            if role == "market":
                if path == "/v1/report" and method == "GET":
                    return report(store.view(), self.auth("owner"))
                if path == "/market/snapshot" and method == "GET":
                    value = store.view()
                    return {
                        "seq": value["seq"],
                        "head": value["head"],
                        "books": value["books"],
                    }
                if path == "/market/deltas" and method == "GET":
                    return store.deltas(integer(int(query.get("after", ["0"])[0])))
                if path == "/market/book" and method == "GET":
                    return levels(
                        store.view(),
                        query.get("symbol", [""])[0],
                        integer(int(query.get("depth", ["10"])[0]), 1, 100),
                    )
                if path == "/market/candles" and method == "GET":
                    value = store.view()
                    return {
                        "seq": value["seq"],
                        "candles": [
                            row
                            for row in candles(
                                value["trades"],
                                integer(
                                    int(query.get("resolution", ["60"])[0]), 1, 86400
                                ),
                            )
                            if row["symbol"] == query.get("symbol", [""])[0]
                        ],
                    }
            raise Error(404, "route missing")

        def handle_request(self):
            self.connection.settimeout(15)
            try:
                # Gateway callbacks from engines must not wait behind a gateway
                # request that is itself awaiting an engine's publication fence.
                with nullcontext() if role == "gateway" else gate:
                    value = self.dispatch()
                status = 200
            except Error as exc:
                value, status = {"error": str(exc)}, exc.status
            except (ValueError, TypeError, KeyError) as exc:
                value, status = {"error": str(exc)}, 400
            except (OSError, TimeoutError, sqlite3.OperationalError):
                value, status = {"error": "unavailable; retry original request_id"}, 503
            except Exception:
                value, status = {"error": "internal failure"}, 500
            raw = encode(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = handle_request
        do_POST = handle_request

    return ThreadingHTTPServer((host, port), Handler)
