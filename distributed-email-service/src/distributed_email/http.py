import base64
import hmac
from http.server import BaseHTTPRequestHandler
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from .common import Error, dumps, http_json, shard_for, text
from .mail import send
from .mailbox_views import MailboxViews
from .objects import Objects
from .routes import decimal, public_route, target
from .server_runtime import Server
from .transport import MAX_REQUEST, length, loads


def server(
    role, config, store, host, port, state=None, *, concurrency=32, request_timeout=15
):
    if role not in ("api", "objects", "smtp", "primary", "replica"):
        raise ValueError("invalid HTTP role")
    config = loads(dumps(config))
    views = MailboxViews(store) if role in ("primary", "replica") else None

    class Handler(BaseHTTPRequestHandler):
        # Apply before parsing headers, including clients that send no request.
        timeout = 10.0

        def log_message(self, *args):
            pass

        def auth(self, role="user"):
            if len(self.headers.get_all("Authorization", [])) != 1:
                raise Error(401, "one role credential required")
            supplied = self.headers.get("Authorization", "")
            if role != "user":
                if hmac.compare_digest(supplied, "Bearer " + config[role + "_token"]):
                    return None
            else:
                for user, token in config["users"].items():
                    if hmac.compare_digest(supplied, "Bearer " + token):
                        return user
            raise Error(401, "valid role credential required")

        def body(self):
            sizes = self.headers.get_all("Content-Length", [])
            if len(sizes) != 1 or self.headers.get_all("Transfer-Encoding", []):
                raise ValueError(
                    "one Content-Length without Transfer-Encoding required"
                )
            size = length(sizes[0])
            if not 0 < size <= MAX_REQUEST:
                raise Error(413, "JSON body must contain 1..16777216 bytes")
            types = self.headers.get_all("Content-Type", [])
            if (
                len(types) != 1
                or types[0].split(";", 1)[0].strip().lower() != "application/json"
            ):
                raise ValueError("application/json body required")
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError("truncated JSON body")
            value = loads(raw)
            if not isinstance(value, dict):
                raise ValueError("JSON object body required")
            return value

        def dispatch(self):
            # BaseHTTPRequestHandler normalizes leading // in self.path.
            path, params = target(self.requestline.split()[1])
            method = self.command
            if len(self.headers.get_all("Content-Length", [])) > 1:
                raise ValueError("duplicate Content-Length")
            if method not in ("POST", "PUT") and (
                self.headers.get_all("Transfer-Encoding", [])
                or any(
                    length(value) != 0
                    for value in self.headers.get_all("Content-Length", [])
                )
            ):
                raise ValueError("request body is unsupported for this method")
            if path == "/health" and method == "GET":
                if params:
                    raise ValueError("health has no query parameters")
                return {"role": role, "local_only": True}
            if role == "objects":
                self.auth("internal")
                if params:
                    raise ValueError("object routes have no query parameters")
                if path == "/objects" and method == "POST":
                    body = self.body()
                    if not isinstance(body, dict) or set(body) != {"data"}:
                        raise ValueError("object requires base64 data")
                    return store.put(base64.b64decode(body["data"], validate=True))
                if path.startswith("/objects/") and method == "GET":
                    data = store.get(path[len("/objects/") :])
                    return {"data": base64.b64encode(data).decode(), "size": len(data)}
                raise Error(404, "unknown object route")
            if role == "smtp":
                self.auth("admin")
                if params:
                    raise ValueError("admin routes have no query parameters")
                if path == "/admin/status" and method == "GET":
                    return store.status()
                if path == "/admin/control" and method == "POST":
                    return store.control(self.body())
                raise Error(404, "unknown spool route")
            if path.startswith("/internal/") and role in ("primary", "replica"):
                self.auth("internal")
                if path == "/internal/status" and method == "GET":
                    if params:
                        raise ValueError("status has no query parameters")
                    return {
                        **store.status(),
                        "replication": state or {},
                        "metadata_cache": views.stats(),
                    }
                if (
                    path == "/internal/changes"
                    and method == "GET"
                    and role == "primary"
                ):
                    if set(params) - {"after"}:
                        raise ValueError("unknown changefeed parameter")
                    return store.changes(decimal(params.get("after", "0"), "after"))
                if role == "primary" and method == "POST":
                    if params:
                        raise ValueError("internal write has no query parameters")
                    body = self.body()
                    if path == "/internal/receive":
                        if set(body) != {"user", "delivery", "descriptor", "body"}:
                            raise ValueError("invalid receive fields")
                        return store.receive(
                            body["user"],
                            body["delivery"],
                            body["descriptor"],
                            body["body"],
                        )
                    if path == "/internal/claim":
                        if body:
                            raise ValueError("claim body must be empty")
                        return {"job": store.claim(lease_seconds=30)}
                    if path == "/internal/settle":
                        if "job" not in body or set(body) - {"job", "error"}:
                            raise ValueError("invalid settlement fields")
                        return {"settled": store.settle(body["job"], body.get("error"))}
                raise Error(404, "unknown internal route")
            if not path.startswith("/v1/"):
                raise Error(404, "unknown route")
            user = self.auth()
            public_route(path, method, params, gateway=role == "api")
            body = self.body() if method in ("POST", "PUT") else None
            if role == "api":
                shard = shard_for(user, len(config["primary_urls"]))
                prefer_replica = params.pop("replica", "false")
                if prefer_replica not in ("true", "false"):
                    raise ValueError("replica must be true/false")
                query = ("?" + urlencode(params)) if params else ""
                token = self.headers["Authorization"][7:]
                if method != "GET" and prefer_replica == "true":
                    raise Error(409, "replicas cannot accept writes")
                urls = (
                    [("replica", config["replica_urls"][shard])]
                    if prefer_replica == "true"
                    else [("primary", config["primary_urls"][shard])]
                )
                if method == "GET" and prefer_replica != "true":
                    urls.append(("replica", config["replica_urls"][shard]))
                last = None
                for source, url in urls:
                    try:
                        value = http_json(
                            url + path + query,
                            token,
                            body,
                            method,
                            deadline=self.server.local.deadline,
                            cancel=self.server.local.cancel,
                        )
                        if not isinstance(value, dict):
                            raise OSError("invalid mailbox response")
                        value["source"] = source
                        return value
                    except HTTPError as exc:
                        if exc.code != 503:
                            raise
                        last = exc
                    except (URLError, OSError, TimeoutError) as exc:
                        last = exc
                raise Error(503, "required mailbox node unavailable") from last
            store.owner(user)
            if "min_position" in params:
                views.ensure(
                    decimal(params.pop("min_position"), "min_position"),
                    params.get("lineage"),
                )
                if path != "/v1/changes":
                    params.pop("lineage", None)
            elif "lineage" in params and path != "/v1/changes":
                raise ValueError("lineage requires min_position")
            if role == "replica":
                if method != "GET":
                    raise Error(409, "replicas are read-only")
                if not store.status()["lineage"]:
                    raise Error(503, "replica not initialized")
            if path == "/v1/uploads" and method == "POST":
                if set(body) != {"filename", "data"}:
                    raise ValueError("upload requires filename and data")
                filename = text(body["filename"], "filename", 200)
                if (
                    not filename
                    or "/" in filename
                    or "\\" in filename
                    or "\r" in filename
                    or "\n" in filename
                ):
                    raise ValueError("attachment filename must be a plain basename")
                data = base64.b64decode(body["data"], validate=True)
                if len(data) > 2 * 1024 * 1024:
                    raise Error(413, "attachment exceeds 2 MiB")
                return store.upload(
                    user,
                    {
                        **Objects(config).put(
                            data,
                            deadline=self.server.local.deadline,
                            cancel=self.server.local.cancel,
                        ),
                        "filename": filename,
                    },
                )
            if path == "/v1/messages":
                if method == "POST":
                    return send(
                        store,
                        config,
                        user,
                        body,
                        deadline=self.server.local.deadline,
                        cancel=self.server.local.cancel,
                    )
                if method == "GET":
                    if set(params) - {"folder", "read", "before", "q", "field"}:
                        raise ValueError("unknown mailbox parameter")
                    if "before" in params:
                        params["before"] = decimal(params["before"], "before")
                    return views.listing(user, **params)
            if path == "/v1/folders" and method == "GET":
                return views.folders(user)
            if path.startswith("/v1/folders/") and method == "GET":
                if "before" in params:
                    params["before"] = decimal(params["before"], "before")
                return views.listing(user, folder=path.split("/")[3], **params)
            if path == "/v1/changes" and method == "GET":
                return views.changes(
                    user,
                    after=decimal(params.get("after", "0"), "after"),
                    lineage=params.get("lineage"),
                    wait=float(params.get("wait", "0")),
                    cancel=self.server.local.cancel,
                )
            parts = path.strip("/").split("/")
            if len(parts) >= 3 and parts[:2] == ["v1", "messages"]:
                mail = parts[2]
                if len(parts) == 3:
                    if method == "GET":
                        value = store.get(user, mail)
                        value["body"] = (
                            Objects(config)
                            .get(
                                value["descriptor"]["body_key"],
                                deadline=self.server.local.deadline,
                                cancel=self.server.local.cancel,
                            )
                            .decode("utf-8")
                        )
                        return value
                    if method == "PUT":
                        if not isinstance(body, dict) or set(body) != {
                            "version",
                            "read",
                        }:
                            raise ValueError("read update requires version and read")
                        return store.update(user, mail, body["version"], body["read"])
                    if method == "DELETE":
                        if set(params) != {"version"}:
                            raise ValueError("delete requires version query parameter")
                        return store.update(
                            user,
                            mail,
                            decimal(params["version"], "version"),
                            delete=True,
                        )
                if len(parts) == 5 and parts[3] == "attachments" and method == "GET":
                    value = store.get(user, mail)
                    if value["descriptor"]["folder"] == "quarantine":
                        raise Error(403, "quarantined attachment download blocked")
                    index = decimal(parts[4], "attachment index")
                    attachments = value["descriptor"]["attachments"]
                    if not 0 <= index < len(attachments):
                        raise Error(404, "attachment missing")
                    attachment = attachments[index]
                    raw = Objects(config).get(
                        attachment["key"],
                        deadline=self.server.local.deadline,
                        cancel=self.server.local.cancel,
                    )
                    return {**attachment, "data": base64.b64encode(raw).decode()}
            raise Error(404, "unknown route or method")

        def handle_request(self):
            try:
                value, status = self.dispatch(), 200
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
            except (URLError, OSError, TimeoutError, sqlite3.OperationalError):
                value, status = {"error": "temporary local service failure"}, 503
            except Exception:
                value, status = {"error": "internal failure"}, 500
            self.close_connection = True
            raw = dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
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
        do_PUT = handle_request
        do_DELETE = handle_request

    return Server(
        (host, port), Handler, concurrency=concurrency, request_timeout=request_timeout
    )
