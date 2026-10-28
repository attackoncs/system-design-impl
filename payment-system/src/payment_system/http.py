import html
import hmac
from http.server import BaseHTTPRequestHandler
from .server_runtime import Server
from .transport import loads, length
from .routes import target, validate_route
import sqlite3
import time

from .common import Error, dumps, signature


def server(role, config, store, host, port, *, concurrency=32, request_timeout=15):
    config = loads(dumps(config))
    if role not in ("api", "provider", "receiver"):
        raise ValueError("invalid HTTP role")

    class Handler(BaseHTTPRequestHandler):
        # Apply before parsing headers, including clients that send no request.
        timeout = 10.0

        def log_message(self, *args):
            pass

        def auth(self, kind):
            supplied = self.headers.get("Authorization", "")
            if (
                len(self.headers.get_all("Authorization", [])) != 1
                or not supplied.isascii()
            ):
                raise Error(401, "one ASCII role credential required")
            if kind == "merchant":
                for merchant, token in config["merchants"].items():
                    if hmac.compare_digest(supplied, "Bearer " + token):
                        return merchant
            elif hmac.compare_digest(supplied, "Bearer " + config[kind + "_token"]):
                return None
            raise Error(401, "valid role credential required")

        def body(self, raw=False, maximum=1024 * 1024):
            sizes = self.headers.get_all("Content-Length", [])
            if len(sizes) != 1 or self.headers.get_all("Transfer-Encoding", []):
                raise ValueError(
                    "one Content-Length without Transfer-Encoding required"
                )
            size = length(sizes[0])
            if not 0 < size <= maximum:
                raise Error(413, "JSON body exceeds request budget")
            types = self.headers.get_all("Content-Type", [])
            if (
                len(types) != 1
                or types[0].split(";", 1)[0].strip().lower() != "application/json"
            ):
                raise ValueError("application/json body required")
            data = self.rfile.read(size)
            if len(data) != size:
                raise ValueError("truncated request")
            value = data if raw else loads(data)
            if not raw and not isinstance(value, dict):
                raise ValueError("JSON object required")
            return value

        def dispatch(self):
            path, params = target(self.requestline.split()[1])
            method = self.command
            validate_route(role, path, method)
            if params:
                raise ValueError("query parameters unsupported")
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
                return {"role": role, "simulated_payments_only": True}
            if role == "receiver":
                if path == "/events" and method == "POST":
                    self.auth("event")
                    return store.receive(self.body())
                self.auth("admin")
                if path == "/admin/status" and method == "GET":
                    return store.status()
                if path == "/admin/control" and method == "POST":
                    return store.control(self.body())
                raise Error(404, "unknown local receiver route")
            if role == "provider":
                if path.startswith("/checkout/"):
                    token = path.split("/")[-1]
                    if method == "POST":
                        body = self.body()
                        if not isinstance(body, dict) or set(body) != {"choice"}:
                            raise ValueError(
                                "simulated checkout only accepts choice; never card details"
                            )
                        return store.checkout(token, body["choice"])
                    if method == "GET":
                        receipt = store.checkout(token)
                        self.content_type = "text/html; charset=utf-8"
                        return (
                            "<!doctype html><html lang='en'><meta charset='utf-8'><title>Simulated checkout</title>"
                            "<body><h1>Simulated checkout</h1><p>No real funds or card details.</p><p>Amount: "
                            + html.escape(str(receipt["amount"]))
                            + " USD cents</p><button data-choice='test-success'>Approve simulation</button>"
                            "<button data-choice='test-decline'>Decline simulation</button><pre id='result'></pre>"
                            "<script>document.querySelectorAll('button').forEach(b=>b.onclick=async()=>{let r=await fetch(location.href,{method:'POST',"
                            "headers:{'Content-Type':'application/json'},body:JSON.stringify({choice:b.dataset.choice})});"
                            "document.getElementById('result').textContent=await r.text()})</script></body></html>"
                        ).encode()
                if path.startswith("/admin/"):
                    self.auth("admin")
                    if path == "/admin/control" and method == "POST":
                        return store.control(self.body())
                    if path == "/admin/status" and method == "GET":
                        return store.status()
                self.auth("provider")
                if path == "/operations" and method == "POST":
                    return store.create(self.body())
                if path.startswith("/operations/") and method == "GET":
                    return store.get(path.split("/")[-1])
                if path == "/statements" and method == "GET":
                    return store.statements()
                raise Error(404, "unknown provider route")
            if path == "/webhooks/mock" and method == "POST":
                raw = self.body(True)
                for name in ("X-Timestamp", "X-Signature"):
                    if len(self.headers.get_all(name, [])) != 1:
                        raise Error(401, "one signature and timestamp required")
                stamp_text = self.headers.get("X-Timestamp", "")
                supplied = self.headers.get("X-Signature", "")
                if (
                    not stamp_text.isascii()
                    or not stamp_text.isdecimal()
                    or len(stamp_text) > 16
                    or not supplied.isascii()
                    or len(supplied) != 64
                ):
                    raise Error(401, "invalid webhook authentication")
                stamp = int(stamp_text)
                if abs(time.time() - stamp) > 300 or not hmac.compare_digest(
                    self.headers.get("X-Signature", ""),
                    signature(config["webhook_secret"], stamp, raw),
                ):
                    raise Error(401, "webhook signature or timestamp invalid")
                body = loads(raw)
                if not isinstance(body, dict) or set(body) != {"event_id", "receipt"}:
                    raise ValueError("invalid callback envelope")
                return store.apply(body["receipt"], body["event_id"])
            if path.startswith("/internal/"):
                self.auth("internal")
                if path.startswith("/internal/checkouts/") and method == "GET":
                    return store.checkout(path.rsplit("/", 1)[1])
                if path.startswith("/internal/operation/") and method == "GET":
                    return store.operation(path.split("/")[-1])
                if method == "POST":
                    body = self.body(
                        maximum=8 * 1024 * 1024
                        if path == "/internal/reconcile"
                        else 1024 * 1024
                    )
                    if path == "/internal/checkouts":
                        return store.create_checkout(body)
                    if path == "/internal/claim":
                        if body:
                            raise ValueError("empty claim body required")
                        return {"job": store.claim(lease_seconds=30)}
                    if path == "/internal/finish":
                        if "job" not in body or set(body) - {"job", "pending", "error"}:
                            raise ValueError("invalid job completion")
                        return {
                            "finished": store.finish_job(
                                body["job"],
                                body.get("pending", False),
                                body.get("error"),
                            )
                        }
                    if path == "/internal/apply":
                        if set(body) != {"receipt"}:
                            raise ValueError("receipt required")
                        return store.apply(body["receipt"])
                    if path == "/internal/reconcile":
                        if set(body) != {"run", "statement", "job"}:
                            raise ValueError("leased reconciliation required")
                        return store.reconcile(
                            body["run"], body["statement"], job=body["job"]
                        )
                raise Error(404, "unknown internal route")
            if path.startswith("/admin/"):
                self.auth("admin")
                if path == "/admin/status" and method == "GET":
                    return store.status()
                if path == "/admin/reconcile" and method == "POST":
                    if self.body():
                        raise ValueError("empty reconcile body required")
                    return store.schedule_reconcile()
                if path == "/admin/retry-job" and method == "POST":
                    body = self.body()
                    if (
                        not isinstance(body, dict)
                        or set(body) != {"job_id"}
                        or not isinstance(body["job_id"], str)
                    ):
                        raise ValueError("job_id required")
                    return store.retry_job(body["job_id"])
                if path.startswith("/admin/reconciliations/") and method == "GET":
                    return store.reconciliation(path.split("/")[-1])
                if path.startswith("/admin/retry/") and method == "POST":
                    if self.body():
                        raise ValueError("empty retry body required")
                    return store.retry(path.split("/")[-1])
                raise Error(404, "unknown administrator route")
            merchant = self.auth("merchant")
            if path == "/v1/wallet" and method == "GET":
                return store.wallet(merchant)
            if path == "/v1/ledger" and method == "GET":
                return store.ledger(merchant)
            if path.startswith("/v1/payments/") and method == "GET":
                value = store.operation(path.rsplit("/", 1)[1], merchant)
                if value["kind"] != "payin":
                    raise Error(404, "payment missing")
                return value
            if path.startswith("/v1/operations/") and method == "GET":
                return store.operation(path.split("/")[-1], merchant)
            if method == "POST" and path in (
                "/v1/payments",
                "/v1/payouts",
                "/v1/refunds",
            ):
                kind = {
                    "/v1/payments": "payin",
                    "/v1/payouts": "payout",
                    "/v1/refunds": "refund",
                }[path]
                return store.create(merchant, kind, self.body())
            raise Error(404, "unknown merchant route")

        def handle_request(self):
            self.content_type = "application/json"
            try:
                value, status = self.dispatch(), 200
            except Error as exc:
                value, status = {"error": str(exc)}, exc.status
            except (ValueError, TypeError, KeyError, RecursionError) as exc:
                value, status = {"error": str(exc)}, 400
            except (OSError, TimeoutError, sqlite3.OperationalError):
                value, status = (
                    {"error": "service unavailable; retry using original request key"},
                    503,
                )
            except Exception:
                value, status = {"error": "internal failure"}, 500
            self.close_connection = True
            raw = value if isinstance(value, bytes) else dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", self.content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except OSError:
                pass

        do_GET = handle_request
        do_POST = handle_request

    return Server(
        (host, port), Handler, concurrency=concurrency, request_timeout=request_timeout
    )
