from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit

from .common import Error, dumps
from .service import aggregate_query, log_page, reconcile


class WorkerLoop:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.stop = threading.Event()
        self.error = "starting"
        self.updated = 0

    def run(self):
        while not self.stop.is_set():
            try:
                batch = log_page(self.config, self.store.partition, self.store.status()["seq"])
                self.store.apply(batch)
                if not batch["entries"]:
                    self.store.idle()
                self.error, self.updated = None, time.time()
                self.stop.wait(0.02 if batch["entries"] else 0.1)
            except Exception as exc:
                # Keep the durable position; never skip corrupt data or an offset gap.
                self.error = str(exc)
                self.stop.wait(0.2)


def server(role, config, store, host, port, loop=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def auth(self, *roles):
            supplied = self.headers.get("Authorization", "")
            allowed = roles + ("admin",)
            if not any(hmac.compare_digest(supplied, "Bearer " + config[r + "_token"]) for r in allowed):
                raise Error(401, "valid bearer credential required")

        def body(self):
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024 * 1024:
                raise Error(413, "JSON request must contain 1..1048576 bytes")
            return json.loads(self.rfile.read(size))

        def dispatch(self):
            target = urlsplit(self.path)
            path = target.path
            params = parse_qs(target.query, keep_blank_values=True)
            if any(len(v) != 1 for v in params.values()):
                raise ValueError("repeated query parameters")
            params = {k: v[0] for k, v in params.items()}
            post = self.command == "POST"
            if path == "/health" and not post:
                return {"role": role, "ready": loop is None or loop.error is None}
            if role == "ingress":
                if path == "/events" and post:
                    self.auth("write")
                    body = self.body()
                    if not isinstance(body, dict) or set(body) != {"events"}:
                        raise ValueError("expected events object")
                    return store.append(body["events"])
                if path == "/internal/log" and not post:
                    self.auth("internal")
                    if set(params) - {"partition", "after", "limit", "until"}:
                        raise ValueError("unknown log parameters")
                    return store.read(**{k: int(v) for k, v in params.items()})
                if path == "/internal/status" and not post:
                    self.auth("internal")
                    return store.status()
            if role == "worker":
                if path == "/internal/status" and not post:
                    self.auth("internal")
                    return {**store.status(), "error": loop.error, "last_poll": loop.updated}
                if path in {"/internal/totals", "/internal/audit"}:
                    self.auth("internal")
                    if loop.error:
                        raise Error(503, "aggregation worker not ready")
                    if path.endswith("totals") and post:
                        return store.totals(self.body())
                    if path.endswith("audit") and not post:
                        return store.audit()
            if role == "api":
                if path == "/admin/reconcile" and post:
                    self.auth("admin")
                    return reconcile(config)
                if path == "/query" and post:
                    self.auth("read")
                    return aggregate_query(config, self.body())
                if not post and path.startswith("/v1/ads/"):
                    self.auth("read")
                    if set(params) - {"from", "to", "window", "count", "user_id", "ip", "country"}:
                        raise ValueError("unknown query parameter")
                    body = {k: int(v) for k, v in params.items() if k in {"from", "to", "window", "count"}}
                    body["filters"] = {k: v for k, v in params.items() if k in {"user_id", "ip", "country"}}
                    if path == "/v1/ads/popular_ads":
                        return aggregate_query(config, body)
                    if path.endswith("/aggregated_count"):
                        body["ad_id"] = unquote(path[len("/v1/ads/"):-len("/aggregated_count")])
                        return aggregate_query(config, body)
            raise Error(404, "unknown route or method")

        def handle_request(self):
            self.connection.settimeout(10)
            try:
                result, status = self.dispatch(), 200
            except Error as exc:
                result, status = {"error": str(exc)}, exc.status
            except (ValueError, TypeError, KeyError) as exc:
                result, status = {"error": str(exc)}, 400
            except HTTPError as exc:
                # Preserve the documented candidate-budget error; fail closed on
                # availability, authentication or topology failures upstream.
                status = 422 if exc.code == 422 else 503
                result = {"error": "candidate budget exceeded" if status == 422 else "required upstream unavailable"}
            except (URLError, TimeoutError, OSError):
                result, status = {"error": "required upstream unavailable"}, 503
            except Exception:
                result, status = {"error": "internal error; input position retained"}, 500
            raw = dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        do_GET = handle_request
        do_POST = handle_request

    return ThreadingHTTPServer((host, port), Handler)
