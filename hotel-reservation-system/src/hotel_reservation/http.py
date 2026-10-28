"""Authenticated explicit routes, bounded admission and owned request shutdown."""

from contextlib import suppress
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
import re
import socket
import sqlite3
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit

from .common import Error, dumps, http_json, identifier, integer, shard_for
from .routes import parameters, public_route, routing_hotel
from .transport import Deadline, MAX_BODY, length, loads, seconds, secret

LOGGER = logging.getLogger(__name__)
MAX_REQUEST = 1024 * 1024


class _Server(ThreadingHTTPServer):
    daemon_threads = False
    request_queue_size = 128

    def __init__(
        self,
        address,
        role,
        config,
        store,
        *,
        concurrency=32,
        request_timeout=10,
        calendar=None,
        workers=None,
    ):
        integer(concurrency, "HTTP capacity", 1, 256)
        if role not in ("gateway", "shard", "payment"):
            raise ValueError("invalid HTTP role")
        config = loads(dumps(config))
        tokens = [
            config[key] for key in ("admin_token", "internal_token", "payment_token")
        ]
        if not isinstance(config.get("users"), dict) or not config["users"]:
            raise ValueError("guest credentials required")
        for user, token in config["users"].items():
            identifier(user)
            tokens.append(token)
        if any(not secret(token) for token in tokens) or len(set(tokens)) != len(
            tokens
        ):
            raise ValueError("distinct private credentials required")
        urls = config.get("shard_urls")
        if (
            not isinstance(urls, list)
            or not 1 <= len(urls) <= 32
            or any(not isinstance(url, str) for url in urls)
            or len(set(urls)) != len(urls)
        ):
            raise ValueError("distinct shard URLs required")
        self.config, self.role, self.store = config, role, store
        self.calendar, self.workers = calendar, workers or {}
        if role == "shard" and calendar is None:
            from .calendar_policy import CalendarPolicies

            self.calendar = CalendarPolicies(store)
        self.request_timeout = seconds(request_timeout)
        self.slots = threading.BoundedSemaphore(concurrency)
        self.stopping, self.local = threading.Event(), threading.local()
        super().__init__(address, _Handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(min(5, self.request_timeout))
        return request, address

    def process_request(self, request, address):
        if self.stopping.is_set() or not self.slots.acquire(False):
            try:
                request.settimeout(0.1)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
                )
                request.shutdown(socket.SHUT_WR)
                _drain(request)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        self.local.cancelled = threading.Event()
        self.local.deadline = time.monotonic() + self.request_timeout

        def interrupt():
            cancelled.set()
            with suppress(OSError):
                request.shutdown(socket.SHUT_RDWR)

        cancelled = self.local.cancelled
        try:
            with Deadline(self.local.deadline, interrupt, self.stopping):
                super().process_request_thread(request, address)
        finally:
            del self.local.cancelled
            del self.local.deadline
            self.slots.release()

    def handle_error(self, request, address):
        cancelled = getattr(self.local, "cancelled", None)
        if (
            cancelled is not None
            and cancelled.is_set()
            and isinstance(sys.exc_info()[1], OSError)
        ):
            return
        LOGGER.warning("hotel HTTP handler failed")

    def server_close(self):
        self.stopping.set()
        super().server_close()


def _drain(sock):
    # Bound accept-loop work and avoid Windows RST overwriting an early response.
    deadline, received = time.monotonic() + 0.1, 0
    while received <= MAX_REQUEST + 65536:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock.settimeout(remaining)
        chunk = sock.recv(min(65536, MAX_REQUEST + 65537 - received))
        if not chunk:
            break
        received += len(chunk)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def handle_expect_100(self):
        return True

    def _live(self):
        if (
            self.server.local.cancelled.is_set()
            or time.monotonic() >= self.server.local.deadline
        ):
            raise TimeoutError("request expired")
        self.connection.settimeout(
            max(0.001, min(5, self.server.local.deadline - time.monotonic()))
        )

    def _header(self, key):
        values = self.headers.get_all(key, [])
        if len(values) > 1:
            raise ValueError("duplicate header")
        return values[0] if values else None

    def identity(self, required="user"):
        supplied = (self._header("Authorization") or "").encode()
        config = self.server.config
        if hmac.compare_digest(supplied, ("Bearer " + config["admin_token"]).encode()):
            if required == "guest":
                raise Error(400, "use a configured guest identity")
            return None
        if required not in ("user", "guest"):
            if hmac.compare_digest(
                supplied, ("Bearer " + config[required + "_token"]).encode()
            ):
                return None
        else:
            for user, token in config["users"].items():
                if hmac.compare_digest(supplied, ("Bearer " + token).encode()):
                    return user
        raise Error(401, "valid role credential required")

    def body(self):
        if self._header("Transfer-Encoding") is not None:
            raise ValueError("Transfer-Encoding unsupported")
        if self._header("Expect") is not None:
            raise Error(417, "Expect unsupported")
        size = length(self._header("Content-Length"))
        if not 0 < size <= MAX_REQUEST:
            raise Error(413, "JSON body must contain 1..1048576 bytes")
        kind = self._header("Content-Type")
        if kind and kind.split(";", 1)[0].strip().lower() != "application/json":
            raise Error(415, "JSON content type required")
        self._live()
        raw = self.rfile.read(size)
        self._live()
        if len(raw) != size:
            raise ValueError("truncated body")
        self.body_consumed = True
        result = loads(raw)
        if not isinstance(result, dict):
            raise ValueError("body must be an object")
        return result

    def no_body(self):
        if (
            self._header("Transfer-Encoding") is not None
            or self._header("Expect") is not None
        ):
            raise ValueError("unsupported framing")
        value = self._header("Content-Length")
        if value is not None and length(value) != 0:
            raise ValueError("body not accepted for this method")
        self.body_consumed = True

    def _internal(self, path, params):
        self.identity("internal")
        store, method = self.server.store, self.command
        if path == "/internal/status" and method == "GET":
            if params:
                raise ValueError("unknown parameter")
            self.no_body()
            return {
                **store.status(),
                "worker_errors": {
                    key: worker.last_error
                    for key, worker in self.server.workers.items()
                },
            }
        if path == "/internal/events" and method == "GET":
            if set(params) - {"after"}:
                raise ValueError("unknown parameter")
            self.no_body()
            after = params.get("after", "0")
            if not re.fullmatch(r"[0-9]{1,16}", after):
                raise ValueError("invalid cursor")
            return store.events(int(after))
        if (
            method != "POST"
            or params
            or path
            not in {
                "/internal/sweep",
                "/internal/claim",
                "/internal/finish",
                "/internal/retry",
                "/internal/calendar-roll",
            }
        ):
            raise Error(404, "unknown internal route")
        body = self.body()
        if path in {"/internal/sweep", "/internal/claim", "/internal/calendar-roll"}:
            if body:
                raise ValueError("empty object required")
            if path == "/internal/sweep":
                return {"expired": store.sweep()}
            if path == "/internal/calendar-roll":
                return self.server.calendar.roll_once()
            return {"job": store.claim(self.server.config.get("lease_seconds", 2))}
        fields = {"job", "result"} if path == "/internal/finish" else {"job", "error"}
        if set(body) != fields:
            raise ValueError("invalid job callback fields")
        if path == "/internal/finish":
            return {"settled": store.finish(body["job"], body["result"])}
        store.retry(body["job"], body["error"])
        return {"retained": True}

    def dispatch(self):
        target = urlsplit(self.path)
        if target.scheme or target.netloc or target.fragment or len(self.path) > 4096:
            raise ValueError("invalid request target")
        params = parameters(target.query)
        method, role, path = self.command, self.server.role, target.path
        if path == "/health" and method == "GET":
            if params:
                raise ValueError("unknown health parameter")
            self.no_body()
            return {
                "role": role,
                "simulated_payments": True,
                "worker_errors": {
                    key: worker.last_error
                    for key, worker in self.server.workers.items()
                },
            }
        if role == "payment":
            if path == "/operations" and method == "POST" and not params:
                self.identity("payment")
                return self.server.store.execute(self.body())
            if path == "/admin/control" and method == "POST" and not params:
                self.identity("admin")
                return self.server.store.control(self.body())
            if path == "/admin/status" and method == "GET" and not params:
                self.identity("admin")
                self.no_body()
                return self.server.store.status()
            raise Error(404, "unknown payment route")
        if role == "shard" and path.startswith("/internal/"):
            return self._internal(path, params)
        route, params = public_route(self.path, method)
        user = self.identity(route.required)
        body = self.body() if method in ("POST", "PUT") else None
        if body is None:
            self.no_body()
        hotel = routing_hotel(route, body, params)
        if role == "gateway":
            url = self.server.config["shard_urls"][
                shard_for(hotel, len(self.server.config["shard_urls"]))
            ]
            token = self._header("Authorization")[len("Bearer ") :]
            return http_json(
                url + self.path,
                token,
                body,
                method,
                timeout=max(0.001, self.server.local.deadline - time.monotonic()),
                deadline=self.server.local.deadline,
                cancel=self.server.local.cancelled,
            )
        store = self.server.store
        store.owner(hotel)
        operation = route.operation
        if operation == "create_hotel":
            return store.manage(
                hotel, {key: value for key, value in body.items() if key != "id"}
            )
        if operation == "hotel":
            return (
                store.catalog(hotel)
                if method == "GET"
                else store.manage(hotel, body, delete=method == "DELETE")
            )
        if operation == "create_room":
            return store.manage(
                hotel,
                {key: value for key, value in body.items() if key != "id"},
                identifier(body["id"]),
            )
        if operation == "room":
            return (
                store.catalog(hotel, route.room)
                if method == "GET"
                else store.manage(hotel, body, route.room, method == "DELETE")
            )
        if operation == "calendar":
            if set(body) != {"days"}:
                raise ValueError("calendar requires days")
            return store.set_days(hotel, route.room, body["days"])
        if operation == "availability":
            return store.availability(hotel, route.room, params["start"], params["end"])
        if operation == "hotel_availability":
            from .catalog_queries import available_rooms

            quantity = params.get("quantity", "1")
            if not re.fullmatch(r"[0-9]{1,2}", quantity):
                raise ValueError("invalid quantity")
            return available_rooms(
                store,
                hotel,
                params["start"],
                params["end"],
                quantity=integer(int(quantity), "quantity", 1, 10),
            )
        if operation == "calendar_policy":
            if method == "GET":
                return self.server.calendar.get(hotel, route.room)
            if method == "DELETE":
                return self.server.calendar.delete(hotel, route.room)
            return self.server.calendar.put(hotel, route.room, body)
        if operation == "reserve":
            return store.reserve(user, body)
        if operation == "history":
            return store.history(hotel, user, params.get("after", ""))
        if operation == "booking":
            return (
                store.booking(route.booking, user)
                if method == "GET"
                else store.cancel(route.booking, user)
            )
        raise Error(404, "unknown route")

    def handle_request(self):
        self.close_connection, self.body_consumed = True, False
        try:
            result, status = self.dispatch(), 200
        except Error as exc:
            result, status = {"error": str(exc)}, exc.status
        except HTTPError as exc:
            status = exc.code if 400 <= exc.code < 500 else 503
            exc.close()
            result = {
                "error": "upstream rejected request"
                if status != 503
                else "service temporarily unavailable; retry with the same key"
            }
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            result, status = {"error": "invalid request"}, 400
        except (OSError, sqlite3.Error):
            result, status = (
                {"error": "service temporarily unavailable; retry with the same key"},
                503,
            )
        except Exception:
            LOGGER.warning("hotel request failed")
            result, status = {"error": "internal error"}, 500
        try:
            try:
                raw = dumps(result).encode()
            except (ValueError, TypeError, OverflowError, RecursionError):
                LOGGER.warning("hotel response serialization failed")
                raw, status = b'{"error":"internal error"}', 500
            if len(raw) > MAX_BODY:
                raw, status = b'{"error":"response exceeds local limit"}', 503
            self._live()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
            if not self.body_consumed:
                self.connection.shutdown(socket.SHUT_WR)
                _drain(self.connection)
        except OSError:
            pass

    do_GET = do_POST = do_PUT = do_DELETE = handle_request


def server(
    role,
    config,
    store,
    host,
    port,
    *,
    concurrency=32,
    request_timeout=10,
    calendar=None,
    workers=None,
):
    return _Server(
        (host, port),
        role,
        config,
        store,
        concurrency=concurrency,
        request_timeout=request_timeout,
        calendar=calendar,
        workers=workers,
    )
