from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from .common import Error, dumps, integer, object_key, request, sha


def server(role, config, store, host, port):
    class Handler(BaseHTTPRequestHandler):
        # Apply before parsing headers, including clients that send no request.
        timeout = 10.0

        def log_message(self, *args):
            pass

        def auth(self, kind="internal"):
            supplied = self.headers.get("Authorization", "")
            if kind == "user":
                for user, token in config["users"].items():
                    if hmac.compare_digest(supplied, "Bearer " + token):
                        return user
            elif hmac.compare_digest(supplied, "Bearer " + config[kind + "_token"]):
                return None
            raise Error(401, "valid role credential required")

        def body(self, raw=False, maximum=4 * 1024 * 1024):
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0 or size > maximum or (not raw and not size):
                raise Error(413, "request size budget exceeded")
            value = self.rfile.read(size)
            if len(value) != size:
                raise ValueError("incomplete request")
            return value if raw else json.loads(value)

        def dispatch(self):
            target = urlsplit(self.path)
            path, method = target.path, self.command
            params = parse_qs(target.query, keep_blank_values=True)
            if any(len(v) != 1 for v in params.values()):
                raise ValueError("repeated query parameter")
            params = {k: v[0] for k, v in params.items()}
            if path == "/health" and method == "GET":
                return {"role": role}
            if role == "data":
                self.auth()
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
                    integer(body["grace"], "grace", 0, 86400)
                    return store.collect(body["live"], body["grace"])
                if path == "/compact" and method == "POST":
                    return store.compact()
            if role == "metadata":
                self.auth()
                if method == "POST":
                    body = self.body()
                    if path == "/publish":
                        return store.publish(**body)
                    if path == "/get":
                        return store.get(**body)
                    if path == "/remove":
                        return store.remove(**body)
                    if path == "/list":
                        return store.listing(**body)
                    if path == "/references":
                        return store.references(**body)
                    if path == "/prune":
                        return store.prune(**body)
            if role != "api":
                raise Error(404, "unknown internal route")
            if path == "/admin/maintenance" and method == "POST":
                self.auth("admin")
                body = self.body()
                if not isinstance(body, dict) or set(body) != {"grace"}:
                    raise ValueError("maintenance requires grace")
                return store.maintenance(body["grace"])
            user = self.auth("user")
            parts = path.strip("/").split("/")
            if parts[:2] == ["v1", "uploads"] and len(parts) >= 3:
                upload = parts[2]
                state = store.catalog.upload(upload, user)
                if len(parts) == 3:
                    if method == "GET":
                        return state
                    if method == "DELETE":
                        if state["state"] == "completing":
                            raise Error(
                                409, "completion intent cannot be aborted; resume it"
                            )
                        if state["state"] == "complete":
                            raise Error(409, "completed upload cannot be aborted")
                        with store.catalog.transaction(True) as db:
                            db.execute(
                                "UPDATE uploads SET state='aborted' WHERE id=?",
                                (upload,),
                            )
                        return {"aborted": upload}
                if len(parts) == 5 and parts[3] == "parts" and method == "PUT":
                    return store.part(
                        upload,
                        user,
                        int(parts[4]),
                        self.body(True, 1024 * 1024),
                        self.headers.get("X-SHA256"),
                    )
                if len(parts) == 4 and parts[3] == "complete" and method == "POST":
                    return store.complete(upload, user)
            if len(parts) >= 3 and parts[:2] == ["v1", "buckets"]:
                name = parts[2]
                if len(parts) == 3 and method == "PUT":
                    return store.catalog.create(name, user)
                bucket = store.catalog.bucket(name, user)
                if len(parts) == 3:
                    if method == "GET":
                        return {**bucket, "versioning": "enabled"}
                    if method == "DELETE":
                        for url in config["metadata_urls"]:
                            if request(
                                url + "/list",
                                config["internal_token"],
                                {"bucket": bucket["id"], "versions": True},
                            )["rows"]:
                                raise Error(
                                    409, "bucket contains versions or delete markers"
                                )
                        with store.catalog.transaction(True) as db:
                            if db.execute(
                                "SELECT 1 FROM uploads WHERE bucket=? AND state IN('active','completing')",
                                (bucket["id"],),
                            ).fetchone():
                                raise Error(409, "bucket has incomplete uploads")
                            db.execute(
                                "DELETE FROM buckets WHERE id=?", (bucket["id"],)
                            )
                        return {"deleted": name}
                if len(parts) == 4:
                    action = parts[3]
                    if action == "objects" and method == "GET":
                        if set(params) - {
                            "prefix",
                            "versions",
                            "limit",
                            "token",
                        } or params.get("versions", "false") not in ("true", "false"):
                            raise ValueError("invalid listing parameters")
                        return store.listing(
                            bucket["id"],
                            user,
                            params.get("prefix", ""),
                            params.get("versions") == "true",
                            int(params.get("limit", 100)),
                            params.get("token"),
                        )
                    if action == "object":
                        if set(params) - {"key", "version"}:
                            raise ValueError("unknown object parameter")
                        key = object_key(params["key"])
                        if method == "PUT":
                            if "version" in params:
                                raise ValueError("cannot overwrite a version")
                            return store.put(
                                bucket["id"],
                                key,
                                self.headers.get("Idempotency-Key"),
                                self.body(True),
                                self.headers.get("X-SHA256"),
                            )
                        if method == "GET":
                            row, data = store.get(
                                bucket["id"], key, params.get("version")
                            )
                            self.object_headers = {
                                "X-Version-ID": row["id"],
                                "X-SHA256": row["sha"],
                            }
                            return data
                        if method == "DELETE":
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
                    if action == "uploads" and method == "POST":
                        return store.catalog.start(bucket["id"], user, self.body())
                    if action == "retention" and method == "POST":
                        body = self.body()
                        if not isinstance(body, dict) or set(body) != {"keep"}:
                            raise ValueError("retention requires keep")
                        integer(body["keep"], "keep", 1, 1000)
                        return {
                            "pruned": sum(
                                request(
                                    url + "/prune",
                                    config["internal_token"],
                                    {"bucket": bucket["id"], "keep": body["keep"]},
                                )["pruned"]
                                for url in config["metadata_urls"]
                            )
                        }
            raise Error(404, "unknown route or method")

        def handle_request(self):
            self.connection.settimeout(15)
            self.object_headers = {}
            try:
                if role == "api":
                    # One catalog coordinator serializes publication/GC to keep
                    # in-flight staging and bucket deletion safe in this reference.
                    with store.catalog.lock:
                        value = self.dispatch()
                else:
                    value = self.dispatch()
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
            except (URLError, OSError, TimeoutError, sqlite3.OperationalError):
                value, status = (
                    {
                        "error": "service unavailable; retry with the same idempotency key"
                    },
                    503,
                )
            except Exception:
                value, status = {"error": "internal failure"}, 500
            raw = value if isinstance(value, bytes) else dumps(value).encode()
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
            self.end_headers()
            self.wfile.write(raw)

        do_GET = handle_request
        do_PUT = handle_request
        do_POST = handle_request
        do_DELETE = handle_request

    return ThreadingHTTPServer((host, port), Handler)
