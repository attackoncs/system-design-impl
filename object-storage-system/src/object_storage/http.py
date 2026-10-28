"""Authenticated HTTP adapters; service layers own persistence and coordination."""

from http.server import BaseHTTPRequestHandler
import hmac
import sqlite3
from urllib.error import HTTPError, URLError

from .common import Error, dumps, integer, object_key, request, sha
from .routes import decimal, public_route, target
from .server_runtime import Server
from .transport import length, loads


def server(role, config, store, host, port, *, concurrency=32, request_timeout=15):
    if role not in ("api", "metadata", "data"):
        raise ValueError("invalid HTTP role")
    config = loads(dumps(config))

    class Handler(BaseHTTPRequestHandler):
        timeout = 5.0

        def log_message(self, *_args):
            pass

        def auth(self, kind="internal"):
            if len(self.headers.get_all("Authorization", [])) != 1:
                raise Error(401, "one role credential required")
            supplied = self.headers["Authorization"]
            if not supplied.isascii():
                raise Error(401, "valid role credential required")
            if kind == "user":
                for user, token in config["users"].items():
                    if hmac.compare_digest(supplied, "Bearer " + token):
                        return user
            elif hmac.compare_digest(supplied, "Bearer " + config[kind + "_token"]):
                return None
            raise Error(401, "valid role credential required")

        def body(self, raw=False, maximum=4 * 1024 * 1024):
            sizes = self.headers.get_all("Content-Length", [])
            if len(sizes) != 1 or self.headers.get_all("Transfer-Encoding", []):
                raise ValueError(
                    "one Content-Length without Transfer-Encoding required"
                )
            size = length(sizes[0])
            if size > maximum or (not raw and size == 0):
                raise Error(413, "request size budget exceeded")
            types = self.headers.get_all("Content-Type", [])
            expected = "application/octet-stream" if raw else "application/json"
            if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != expected:
                raise ValueError(expected + " body required")
            value = self.rfile.read(size)
            if len(value) != size:
                raise ValueError("incomplete request")
            if raw:
                return value
            value = loads(value)
            if not isinstance(value, dict):
                raise ValueError("JSON object body required")
            return value

        def no_body(self):
            if self.headers.get_all("Transfer-Encoding", []) or any(
                length(value) != 0
                for value in self.headers.get_all("Content-Length", [])
            ):
                raise ValueError("request body unsupported for this route")

        def rpc(self, url, body):
            return request(
                url,
                config["internal_token"],
                body,
                deadline=self.server.local.deadline,
                cancel=self.server.local.cancel,
            )

        def dispatch(self):
            path, params = target(self.requestline.split()[1])
            method = self.command
            if any(
                len(self.headers.get_all(name, [])) > 1
                for name in ("Host", "X-SHA256", "Idempotency-Key")
            ):
                raise ValueError("duplicate singleton request header")
            if len(
                self.headers.get_all("Content-Length", [])
            ) > 1 or self.headers.get_all("Transfer-Encoding", []):
                raise ValueError("ambiguous request framing")
            if method in ("GET", "DELETE"):
                self.no_body()
            if path == "/health" and method == "GET":
                if params:
                    raise ValueError("health has no query parameters")
                return {"role": role}
            if role == "data":
                self.auth()
                if params:
                    raise ValueError("data routes have no query parameters")
                if path.startswith("/chunks/"):
                    key = path[len("/chunks/") :]
                    if method == "GET":
                        return store.get(key)
                    if method == "PUT":
                        return store.put(key, self.body(True, 256 * 1024))
                if path == "/status" and method == "GET":
                    return store.status()
                if path == "/collect" and method == "POST":
                    body = self.body()
                    if set(body) != {"live", "grace", "generation"}:
                        raise ValueError("collect requires live, grace and generation")
                    integer(body["grace"], "grace", 0, 86400)
                    return store.collect(
                        body["live"], body["grace"], generation=body["generation"]
                    )
                if path == "/compact" and method == "POST":
                    if self.body():
                        raise ValueError("compact body must be empty")
                    return store.compact()
                raise Error(404, "unknown data route")
            if role == "metadata":
                self.auth()
                if params:
                    raise ValueError("metadata routes have no query parameters")
                functions = {
                    "/publish": store.publish,
                    "/get": store.get,
                    "/remove": store.remove,
                    "/list": store.listing,
                    "/references": store.references,
                    "/prune": store.prune,
                }
                if method == "POST" and path in functions:
                    return functions[path](**self.body())
                raise Error(404, "unknown metadata route")
            if path == "/admin/maintenance" and method == "POST":
                self.auth("admin")
                if params:
                    raise ValueError("maintenance has no query parameters")
                self.server.set_budget(60)
                body = self.body()
                if set(body) != {"grace"}:
                    raise ValueError("maintenance requires grace")
                with (
                    store.operation(
                        deadline=self.server.local.deadline,
                        cancel=self.server.local.cancel,
                    ),
                    store.guard(method, path),
                ):
                    return store.maintenance(body["grace"])
            user = self.auth("user")
            public_route(path, method, params)
            with (
                store.operation(
                    deadline=self.server.local.deadline, cancel=self.server.local.cancel
                ),
                store.guard(method, path),
            ):
                return self.api_dispatch(path, method, params, user)

        def api_dispatch(self, path, method, params, user):
            if path == "/v1/buckets":
                return store.catalog.listing(user)
            parts = path.strip("/").split("/")
            if parts[:2] == ["v1", "uploads"]:
                upload = parts[2]
                if len(parts) == 3:
                    if method == "GET":
                        return store.catalog.upload(upload, user)
                    return store.abort(upload, user)
                if len(parts) == 5:
                    return store.part(
                        upload,
                        user,
                        decimal(parts[4], "part number"),
                        self.body(True, 1024 * 1024),
                        self.headers.get("X-SHA256"),
                    )
                if self.body():
                    raise ValueError("complete body must be empty")
                return store.complete(upload, user)
            name = parts[2]
            if len(parts) == 3 and method == "PUT":
                self.no_body()
                return store.catalog.create(name, user)
            bucket = store.catalog.bucket(name, user)
            if len(parts) == 3:
                if method == "GET":
                    return {**bucket, "versioning": "enabled"}
                for url in config["metadata_urls"]:
                    if self.rpc(
                        url + "/list", {"bucket": bucket["id"], "versions": True}
                    )["rows"]:
                        raise Error(409, "bucket contains versions or delete markers")
                with store.catalog.transaction(True) as db:
                    if db.execute(
                        "SELECT 1 FROM publications WHERE bucket=? AND done=0 LIMIT 1",
                        (bucket["id"],),
                    ).fetchone():
                        raise Error(
                            409,
                            "bucket has pending publications; recover before deleting",
                        )
                    if db.execute(
                        "SELECT 1 FROM uploads WHERE bucket=? AND state IN('active','completing')",
                        (bucket["id"],),
                    ).fetchone():
                        raise Error(409, "bucket has incomplete uploads")
                    db.execute("DELETE FROM buckets WHERE id=?", (bucket["id"],))
                return {"deleted": name}
            action = parts[3]
            if action == "objects":
                if params.get("versions", "false") not in ("true", "false"):
                    raise ValueError("versions must be true/false")
                return store.listing(
                    bucket["id"],
                    user,
                    params.get("prefix", ""),
                    params.get("versions") == "true",
                    decimal(params.get("limit", "100"), "limit"),
                    params.get("token"),
                    delimiter=params.get("delimiter", ""),
                )
            if action == "object":
                key = object_key(params["key"])
                if method == "PUT":
                    return store.put(
                        bucket["id"],
                        key,
                        self.headers.get("Idempotency-Key"),
                        self.body(True),
                        self.headers.get("X-SHA256"),
                    )
                if method == "GET":
                    row, data = store.get(bucket["id"], key, params.get("version"))
                    self.object_headers = {
                        "X-Version-ID": row["id"],
                        "X-SHA256": row["sha"],
                    }
                    return data
                if "version" in params:
                    return store.meta(
                        bucket["id"],
                        key,
                        "/remove",
                        {
                            "bucket": bucket["id"],
                            "key": key,
                            "version": params["version"],
                        },
                    )
                return store.publish(
                    bucket["id"],
                    key,
                    self.headers.get("Idempotency-Key"),
                    [],
                    0,
                    sha(b""),
                    True,
                )
            if action == "uploads":
                return store.catalog.start(bucket["id"], user, self.body())
            body = self.body()
            if set(body) != {"keep"}:
                raise ValueError("retention requires keep")
            integer(body["keep"], "keep", 1, 1000)
            return {
                "pruned": sum(
                    self.rpc(
                        url + "/prune", {"bucket": bucket["id"], "keep": body["keep"]}
                    )["pruned"]
                    for url in config["metadata_urls"]
                )
            }

        def handle_request(self):
            self.object_headers = {}
            try:
                value, status = self.dispatch(), 200
            except Error as exc:
                value, status = {"error": str(exc)}, exc.status
            except HTTPError as exc:
                try:
                    value, status = loads(exc.read(1024 * 1024)), exc.code
                    if not isinstance(value, dict):
                        value = {"error": "upstream failure"}
                except (ValueError, OSError):
                    value, status = {"error": "upstream failure"}, 503
                finally:
                    exc.close()
            except (ValueError, TypeError, KeyError, RecursionError) as exc:
                value, status = {"error": str(exc)}, 400
            except (URLError, OSError, TimeoutError, sqlite3.OperationalError):
                value, status = (
                    {
                        "error": "service unavailable; retry with the same idempotency key"
                    },
                    503,
                )
            except Exception:
                value, status = {"error": "internal failure"}, 500
            self.close_connection = True
            raw = value if isinstance(value, bytes) else dumps(value).encode()
            try:
                self.send_response(status)
                self.send_header(
                    "Content-Type",
                    "application/octet-stream"
                    if isinstance(value, bytes)
                    else "application/json",
                )
                self.send_header("Content-Length", str(len(raw)))
                for key, val in self.object_headers.items():
                    self.send_header(key, val)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)
            except OSError:
                pass

        do_GET = handle_request
        do_PUT = handle_request
        do_POST = handle_request
        do_DELETE = handle_request

    return Server(
        (host, port), Handler, concurrency=concurrency, request_timeout=request_timeout
    )
