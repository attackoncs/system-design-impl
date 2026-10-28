from abc import ABC, abstractmethod
import hmac
from http.server import BaseHTTPRequestHandler
import threading
import sqlite3

from .common import Error, encode, integer, request
from .contracts import fields, freeze, MAX_TIMESTAMP
from .routes import target
from .server_runtime import Server
from .transport import loads, length
from .admission import OwnerRate
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


def server(
    role, store, config, gate, host, port, *, concurrency=32, request_timeout=15
):
    config = freeze(config)
    identity = StaticIdentity(config["owners"])
    public_slots = threading.BoundedSemaphore(16)
    rate = OwnerRate(config["owners"])

    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def log_message(self, *args):
            pass

        def auth(self, kind):
            supplied = self.headers.get("Authorization", "")
            if (
                len(self.headers.get_all("Authorization", [])) != 1
                or not supplied.isascii()
            ):
                raise Error(401, "one ASCII role credential required")
            if kind == "owner":
                return identity.owner(supplied)
            if not hmac.compare_digest(supplied, "Bearer " + config[kind + "_token"]):
                raise Error(401, "role credential required")

        def body(self):
            sizes = self.headers.get_all("Content-Length", [])
            if len(sizes) != 1 or self.headers.get_all("Transfer-Encoding", []):
                raise ValueError(
                    "one Content-Length without Transfer-Encoding required"
                )
            size = length(sizes[0])
            if not 0 < size <= 1024 * 1024:
                raise Error(413, "JSON body must be 1..1048576 bytes")
            types = self.headers.get_all("Content-Type", [])
            if (
                len(types) != 1
                or types[0].split(";", 1)[0].strip().lower() != "application/json"
            ):
                raise ValueError("application/json required")
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError("truncated request")
            value = loads(raw)
            if not isinstance(value, dict):
                raise ValueError("JSON object required")
            return value

        def rpc(self, url, body=None):
            return request(
                url,
                config["internal_token"],
                body,
                timeout=2,
                deadline=self.server.local.deadline,
                cancel=self.server.local.cancel,
            )

        def active(self, kind, key, owner):
            control = store.control()
            value = self.rpc(
                config["engines"][control["active"]]
                + "/internal/query?epoch="
                + str(control["epoch"])
                + "&kind="
                + kind
                + "&key="
                + key
                + "&owner="
                + owner
            )
            fields(value, {"seq", "head", "epoch", "kind", "key", "owner", "value"})
            current = store.control()
            integer(value["seq"], 1, current["seq"])
            if (
                (current["epoch"], current["active"])
                != (control["epoch"], control["active"])
                or value["epoch"] != control["epoch"]
                or (value["kind"], value["key"], value["owner"]) != (kind, key, owner)
            ):
                raise Error(409, "active matching epoch/query changed")
            rows = store.events(value["seq"] - 1, 1)
            if not rows or rows[0]["hash"] != value["head"]:
                raise Error(502, "engine response is outside sequencer history")
            return value

        def published(self, query, callback):
            control = self.rpc(config["gateway"] + "/internal/control")
            if control["active"] != role or query.get("epoch", 0) != control["epoch"]:
                raise Error(409, "matching publisher fenced by active epoch")
            store.fence(control["epoch"])
            value = callback()
            current = self.rpc(config["gateway"] + "/internal/control")
            if (current["epoch"], current["active"]) != (control["epoch"], role):
                raise Error(409, "matching publisher changed during read")
            store.fence(control["epoch"])
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
                raise ValueError("ambiguous/unused request body")
            if path == "/health":
                return {"role": role, "synthetic_orders_only": True}
            if path.startswith("/admin/"):
                self.auth("admin")
                if role == "gateway":
                    if path == "/admin/status":
                        return store.control()
                    if path == "/admin/clock":
                        return store.clock(
                            fields(self.body(), {"timestamp"})["timestamp"]
                        )
                    if path == "/admin/promote":
                        node = fields(self.body(), {"node"})["node"]
                        if node not in config["engines"]:
                            raise ValueError("unknown matching node")
                        return store.promote(
                            node, self.rpc(config["engines"][node] + "/internal/raw")
                        )
                if role in ("primary", "backup"):
                    if path == "/admin/snapshot":
                        fields(self.body(), set())
                        return store.snapshot()
                    return store.audit()
                value = store.view()
                return {key: value[key] for key in ("seq", "head", "resyncs", "epoch")}
            if path.startswith("/internal/"):
                self.auth("internal")
                if role == "gateway":
                    if path == "/internal/control":
                        return store.control()
                    return {"events": store.events(query.get("after", 0))}
                if path == "/internal/raw":
                    return store.raw()
                if path == "/internal/serve":
                    return self.published(query, store.raw)
                if path == "/internal/feed":
                    return self.published(
                        query, lambda: store.feed(query.get("after", 0))
                    )

                def result():
                    value = store.query(
                        query.get("kind", ""),
                        query.get("key", ""),
                        query.get("owner", ""),
                    )
                    return {**value, "epoch": query["epoch"]}

                return self.published(query, result)
            if role == "gateway":
                owner = self.auth("owner")
                if method == "POST":
                    rate.take(owner)
                    body = self.body()
                    if path == "/v1/order":
                        fields(
                            body,
                            {
                                "request_id",
                                "order_id",
                                "symbol",
                                "side",
                                "price",
                                "quantity",
                                "orderType",
                            },
                        )
                        if body.pop("orderType") != "limit":
                            raise ValueError("only limit orderType supported")
                    return store.submit(
                        owner, "cancel" if path == "/v1/cancels" else "new", body
                    )
                if path.startswith("/v1/requests/"):
                    sequence = store.lookup(owner, path.split("/")[-1])
                    return self.active("request", str(sequence), owner)["value"]
                if path == "/v1/account":
                    value = self.active("account", owner, owner)
                    return {"seq": value["seq"], **value["value"]}
                return self.active("order", path.split("/")[-1], owner)["value"]
            if path in ("/v1/report", "/execution", "/v1/executions"):
                owner = self.auth("owner")
                value = store.view()
                if path == "/v1/report":
                    return report(value, owner)
                return {
                    "seq": value["seq"],
                    "executions": store.executions(value, owner, query),
                }
            if path == "/market/deltas":
                return store.deltas(query.get("after", 0))
            value = store.view()
            if path == "/market/snapshot":
                return {key: value[key] for key in ("seq", "head", "books")}
            if path in ("/market/book", "/marketdata/orderBook/L2"):
                return levels(value, query.get("symbol", ""), query.get("depth", 10))
            symbol = query.get("symbol", "")
            if symbol not in value["books"]:
                raise Error(404, "symbol missing")
            return {
                "seq": value["seq"],
                "candles": [
                    row
                    for row in candles(value["trades"], query.get("resolution", 60))
                    if row["symbol"] == symbol
                    and query.get("startTime", 0)
                    <= row["start"]
                    < query.get("endTime", MAX_TIMESTAMP)
                ],
            }

        def handle_request(self):
            self.close_connection = True
            self.connection.settimeout(min(10, request_timeout))
            admitted = False
            try:
                if (
                    role == "gateway"
                    and not self.path.startswith("/internal/")
                    and self.path != "/health"
                ):
                    admitted = public_slots.acquire(False)
                    if not admitted:
                        raise Error(503, "gateway public capacity exhausted")
                value, status = self.dispatch(), 200
            except Error as exc:
                value, status = {"error": str(exc)}, exc.status
            except (ValueError, TypeError, KeyError, RecursionError) as exc:
                value, status = {"error": str(exc)}, 400
            except (OSError, TimeoutError, sqlite3.OperationalError):
                value, status = {"error": "unavailable; retry original request_id"}, 503
            except Exception:
                value, status = {"error": "internal failure"}, 500
            finally:
                if admitted:
                    public_slots.release()
            raw = encode(value).encode()
            if len(raw) > 8 * 1024 * 1024:
                status, raw = 503, b'{"error":"response exceeds 8 MiB budget"}'
            self.send_response(status)
            for key, item in (
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(raw))),
                ("Cache-Control", "no-store"),
                ("Connection", "close"),
            ):
                self.send_header(key, item)
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
