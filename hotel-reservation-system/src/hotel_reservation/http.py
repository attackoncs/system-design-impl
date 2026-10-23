from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from .common import Error, booking_hotel, dumps, http_json, identifier, shard_for


def server(role, config, store, host, port):
    class Handler(BaseHTTPRequestHandler):
        # Apply before parsing headers, including clients that send no request.
        timeout = 10.0

        def log_message(self, *args):
            pass

        def identity(self, required="user"):
            supplied = self.headers.get("Authorization", "")
            if hmac.compare_digest(supplied, "Bearer " + config["admin_token"]):
                return None
            if required != "user":
                if hmac.compare_digest(
                    supplied, "Bearer " + config[required + "_token"]
                ):
                    return None
            else:
                for user, token in config["users"].items():
                    if hmac.compare_digest(supplied, "Bearer " + token):
                        return user
            raise Error(401, "valid role credential required")

        def body(self):
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024 * 1024:
                raise Error(413, "JSON body must contain 1..1048576 bytes")
            return json.loads(self.rfile.read(size))

        def dispatch(self):
            target = urlsplit(self.path)
            path, method = target.path, self.command
            if path == "/health" and method == "GET":
                return {"role": role, "simulated_payments": True}
            params = parse_qs(target.query, keep_blank_values=True)
            if any(len(v) != 1 for v in params.values()):
                raise ValueError("repeated query parameter")
            params = {k: v[0] for k, v in params.items()}
            if role == "payment":
                if path == "/operations" and method == "POST":
                    self.identity("payment")
                    return store.execute(self.body())
                if path == "/admin/control" and method == "POST":
                    self.identity("admin")
                    return store.control(self.body())
                if path == "/admin/status" and method == "GET":
                    self.identity("admin")
                    return store.status()
                raise Error(404, "unknown payment route")
            if role == "shard" and path.startswith("/internal/"):
                self.identity("internal")
                if path == "/internal/status" and method == "GET":
                    return store.status()
                if path == "/internal/events" and method == "GET":
                    return store.events(int(params.get("after", 0)))
                if method == "POST":
                    body = self.body()
                    if path == "/internal/sweep":
                        return {"expired": store.sweep()}
                    if path == "/internal/claim":
                        return {"job": store.claim(config.get("lease_seconds", 2))}
                    if path == "/internal/finish":
                        return {"settled": store.finish(body["job"], body["result"])}
                    if path == "/internal/retry":
                        store.retry(body["job"], body["error"])
                        return {"retained": True}
                raise Error(404, "unknown internal route")
            if not path.startswith("/v1/"):
                raise Error(404, "unknown route")
            user = self.identity()
            body = self.body() if method in ("POST", "PUT") else None
            parts = path.strip("/").split("/")
            hotel = None
            if parts[:2] == ["v1", "hotels"]:
                hotel = (
                    identifier(parts[2]) if len(parts) > 2 else identifier(body["id"])
                )
            elif parts[:2] == ["v1", "reservations"]:
                if len(parts) == 3:
                    hotel = booking_hotel(parts[2])
                else:
                    hotel = identifier(
                        body["hotel"] if method == "POST" else params["hotel"]
                    )
            if hotel is None:
                raise Error(404, "unknown resource")
            if role == "gateway":
                url = config["shard_urls"][shard_for(hotel, len(config["shard_urls"]))]
                token = self.headers["Authorization"][len("Bearer ") :]
                return http_json(url + self.path, token, body, method)
            store.owner(hotel)
            if parts[:2] == ["v1", "hotels"]:
                if len(parts) == 2 and method == "POST":
                    self.identity("admin")
                    return store.manage(
                        hotel, {k: v for k, v in body.items() if k != "id"}
                    )
                if len(parts) == 3:
                    if method == "GET":
                        return store.catalog(hotel)
                    self.identity("admin")
                    if method in ("PUT", "DELETE"):
                        return store.manage(hotel, body, delete=method == "DELETE")
                if len(parts) >= 4 and parts[3] == "rooms":
                    room = (
                        identifier(parts[4])
                        if len(parts) > 4
                        else identifier(body["id"])
                    )
                    if len(parts) == 4 and method == "POST":
                        self.identity("admin")
                        return store.manage(
                            hotel, {k: v for k, v in body.items() if k != "id"}, room
                        )
                    if len(parts) == 5:
                        if method == "GET":
                            return store.catalog(hotel, room)
                        self.identity("admin")
                        if method in ("PUT", "DELETE"):
                            return store.manage(hotel, body, room, method == "DELETE")
                    if len(parts) == 6 and parts[5] == "calendar" and method == "PUT":
                        self.identity("admin")
                        if not isinstance(body, dict) or set(body) != {"days"}:
                            raise ValueError("calendar requires days")
                        return store.set_days(hotel, room, body["days"])
                    if (
                        len(parts) == 6
                        and parts[5] == "availability"
                        and method == "GET"
                    ):
                        if set(params) != {"start", "end"}:
                            raise ValueError("availability requires start and end")
                        return store.availability(
                            hotel, room, params["start"], params["end"]
                        )
            if parts[:2] == ["v1", "reservations"]:
                if len(parts) == 2:
                    if method == "POST":
                        if user is None:
                            raise Error(
                                400, "reserve using a configured guest identity"
                            )
                        return store.reserve(user, body)
                    if method == "GET":
                        if user is None:
                            raise Error(400, "history requires a guest identity")
                        return store.history(hotel, user, params.get("after", ""))
                if len(parts) == 3:
                    if method == "GET":
                        return store.booking(parts[2], user)
                    if method == "DELETE":
                        return store.cancel(parts[2], user)
            raise Error(404, "unknown route or method")

        def handle_request(self):
            self.connection.settimeout(10)
            try:
                result, status = self.dispatch(), 200
            except Error as exc:
                result, status = {"error": str(exc)}, exc.status
            except HTTPError as exc:
                try:
                    raw = exc.read(1024 * 1024)
                    result = json.loads(raw)
                    status = exc.code
                except Exception:
                    result, status = {"error": "upstream rejected request"}, 503
            except (ValueError, TypeError, KeyError) as exc:
                result, status = {"error": str(exc)}, 400
            except (URLError, OSError, TimeoutError, sqlite3.OperationalError):
                result, status = (
                    {
                        "error": "service temporarily unavailable; retry with the same key"
                    },
                    503,
                )
            except Exception:
                result, status = {"error": "internal error"}, 500
            raw = dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        do_GET = handle_request
        do_POST = handle_request
        do_PUT = handle_request
        do_DELETE = handle_request

    return ThreadingHTTPServer((host, port), Handler)
