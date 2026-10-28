import hmac
from http.server import BaseHTTPRequestHandler
import sqlite3
from contextlib import nullcontext

from .common import Error, encode
from .contracts import fields, freeze
from .routes import target
from .server_runtime import Server
from .transport import loads, length


def server(role, store, config, host, port, *, concurrency=32, request_timeout=15):
    config = freeze(config)
    if role == "shard":
        store.initialize(
            "CREATE TABLE IF NOT EXISTS fault(id INTEGER PRIMARY KEY CHECK(id=1),lose_reply INTEGER,reject_prepare INTEGER); INSERT OR IGNORE INTO fault VALUES(1,0,0);"
        )
    if role == "replica":
        store.initialize(
            "CREATE TABLE IF NOT EXISTS fault(id INTEGER PRIMARY KEY CHECK(id=1),reject_append INTEGER); INSERT OR IGNORE INTO fault VALUES(1,0);"
        )

    class Handler(BaseHTTPRequestHandler):
        # Apply before parsing headers, including clients that send no request.
        timeout = 10.0

        def log_message(self, *args):
            pass

        def auth(self, kind):
            value = self.headers.get("Authorization", "")
            if (
                len(self.headers.get_all("Authorization", [])) != 1
                or not value.isascii()
            ):
                raise Error(401, "one ASCII role credential required")
            if kind == "owner":
                for owner, token in config["owners"].items():
                    if hmac.compare_digest(value, "Bearer " + token):
                        return owner
            elif hmac.compare_digest(value, "Bearer " + config[kind + "_token"]):
                return
            raise Error(401, "role credential required")

        def body(self):
            sizes = self.headers.get_all("Content-Length", [])
            if len(sizes) != 1 or self.headers.get_all("Transfer-Encoding", []):
                raise ValueError(
                    "one Content-Length without Transfer-Encoding required"
                )
            size = length(sizes[0])
            if not 0 < size <= 1024 * 1024:
                raise Error(413, "body must contain 1..1048576 bytes")
            types = self.headers.get_all("Content-Type", [])
            if (
                len(types) != 1
                or types[0].split(";", 1)[0].strip().lower() != "application/json"
            ):
                raise ValueError("application/json body required")
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError("truncated body")
            value = loads(raw)
            if not isinstance(value, dict):
                raise ValueError("object body required")
            return value

        def dispatch(self):
            path, query = target(self.requestline.split()[1], role, self.command)
            method = self.command
            sizes = self.headers.get_all("Content-Length", [])
            if len(sizes) > 1 or (
                method == "GET"
                and (
                    self.headers.get_all("Transfer-Encoding", [])
                    or any(length(size) != 0 for size in sizes)
                )
            ):
                raise ValueError("ambiguous or unused request body")
            if path == "/health" and method == "GET":
                return {"role": role, "synthetic_funds_only": True}
            if path.startswith("/admin/"):
                self.auth("admin")
                if (
                    role in ("shard", "replica")
                    and path == "/admin/fence"
                    and method == "POST"
                ):
                    body = fields(self.body(), {"epoch", "leader"})
                    return store.fence(body["epoch"], body["leader"])
                if role == "replica" and path == "/admin/control" and method == "POST":
                    body = self.body()
                    if (
                        set(body) != {"reject_append"}
                        or type(body["reject_append"]) is not bool
                    ):
                        raise ValueError("boolean reject_append required")
                    with store.transaction() as db:
                        db.execute(
                            "UPDATE fault SET reject_append=?", (body["reject_append"],)
                        )
                    return {"updated": True}
                if role == "shard":
                    if path == "/admin/snapshot" and method == "POST":
                        fields(self.body(), set())
                        return store.snapshot()
                    if path == "/admin/control" and method == "POST":
                        body = self.body()
                        if set(body) != {"lose_reply", "reject_prepare"} or any(
                            type(value) is not bool for value in body.values()
                        ):
                            raise ValueError("boolean fault flags required")
                        with store.transaction() as db:
                            db.execute(
                                "UPDATE fault SET lose_reply=?,reject_prepare=?",
                                (body["lose_reply"], body["reject_prepare"]),
                            )
                        return {"updated": True}
                if role == "coordinator":
                    if path == "/admin/status" and method == "GET":
                        return store.status()
                    if path == "/admin/audit" and method == "GET":
                        return store.audit()
                    if path == "/admin/sync" and method == "POST":
                        fields(self.body(), set())
                        return store.sync()
                    if path.startswith("/admin/retry/") and method == "POST":
                        fields(self.body(), set())
                        return store.retry(path.split("/")[-1])
                    if path.startswith("/admin/cancel/") and method == "POST":
                        fields(self.body(), set())
                        return store.cancel(path.split("/")[-1])
                raise Error(404, "unknown admin route")
            if path.startswith("/internal/"):
                self.auth("internal")
                if role == "replica":
                    if path == "/internal/status" and method == "POST":
                        body = fields(self.body(), {"epoch", "leader"})
                        return store.status(body["epoch"], body["leader"])
                    if path == "/internal/append" and method == "POST":
                        body = fields(self.body(), {"entry", "epoch", "leader"})
                        with store.connection() as db:
                            if db.execute("SELECT reject_append FROM fault").fetchone()[
                                0
                            ]:
                                raise Error(503, "simulated replica append outage")
                        return store.append(
                            body["entry"], body["epoch"], body["leader"]
                        )
                if role == "shard":
                    if path == "/internal/view" and method == "GET":
                        return store.view(query.get("at"))
                    if path.startswith("/internal/accounts/") and method == "GET":
                        return store.balance(path.split("/")[-1], query.get("at"))
                    if path.startswith("/internal/participants/") and method == "GET":
                        return {"participant": store.participant(path.split("/")[-1])}
                    if path == "/internal/audit" and method == "GET":
                        return store.audit()
                    if path == "/internal/action" and method == "POST":
                        body = fields(
                            self.body(), {"intent", "kind", "role", "epoch", "leader"}
                        )
                        with store.connection() as db:
                            fault = dict(db.execute("SELECT * FROM fault").fetchone())
                        if fault["reject_prepare"] and body["kind"] == "prepare":
                            raise Error(422, "simulated definitive prepare rejection")
                        result = store.action(
                            body["intent"],
                            body["kind"],
                            body["role"],
                            body["epoch"],
                            body["leader"],
                        )
                        if fault["lose_reply"]:
                            raise Error(
                                503,
                                "simulated reply lost after durable participant commit",
                            )
                        return result
                if (
                    role == "coordinator"
                    and path == "/internal/step"
                    and method == "POST"
                ):
                    fields(self.body(), set())
                    return store.step()
                raise Error(404, "unknown internal route")
            if role == "coordinator":
                owner = self.auth("owner")
                if path == "/v1/wallet/balance_transfer" and method == "POST":
                    value = store.create(self.body(), owner)
                    return store.wait_owned(
                        value["intent"]["transaction_id"],
                        owner,
                        query.get("wait_ms", 0),
                    )
                if path.startswith("/v1/transfers/") and method == "GET":
                    return store.wait_owned(
                        path.split("/")[-1], owner, query.get("wait_ms", 0)
                    )
                if path.startswith("/v1/accounts/") and method == "GET":
                    return store.balance(path.split("/")[-1], owner, query.get("at"))
            raise Error(404, "unknown route")

        def handle_request(self):
            self.close_connection = True
            self.connection.settimeout(min(10, request_timeout))
            try:
                context = (
                    store.runtime.operation(
                        self.server.local.deadline, self.server.local.cancel
                    )
                    if role == "coordinator"
                    else nullcontext()
                )
                with context:
                    value = self.dispatch()
                status = 200
            except Error as exc:
                value, status = {"error": str(exc)}, exc.status
            except (ValueError, KeyError, TypeError, RecursionError) as exc:
                value, status = {"error": str(exc)}, 400
            except (OSError, TimeoutError, sqlite3.OperationalError):
                value, status = (
                    {"error": "temporarily unavailable; retry original transaction_id"},
                    503,
                )
            except Exception:
                value, status = {"error": "internal failure"}, 500
            raw = encode(value).encode()
            if len(raw) > 8 * 1024 * 1024:
                status, raw = (
                    503,
                    b'{"error":"response exceeds 8 MiB reference budget"}',
                )
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
