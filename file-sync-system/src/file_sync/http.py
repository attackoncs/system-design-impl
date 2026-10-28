"""Bounded file APIs and durable long polls with separate admission limits.

Absolute deadlines cover socket I/O. Metadata/storage calls may finish after a
client disconnects; accepted mutations still require reconciliation by the client.
"""

from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import re
import socket
import sqlite3
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .service import Conflict
from .storage import ArchivedBlock

_JSON_LIMIT = 2 * 1024**2
_ROUTES = {
    ("GET", "/config"): ("config", set(), set()),
    ("GET", "/upload"): ("upload_status", {"upload"}, set()),
    ("GET", "/metadata"): ("metadata", {"file"}, {"revision"}),
    ("GET", "/files"): ("files", {"space"}, set()),
    ("GET", "/revisions"): ("revisions", {"file"}, {"limit"}),
    ("GET", "/block"): ("download_block", {"file", "revision", "index"}, set()),
    ("GET", "/changes"): ("changes", {"device"}, {"cursor", "wait", "limit"}),
    ("PUT", "/block"): ("upload_block", {"upload", "digest"}, set()),
    ("PUT", "/file"): ("simple_upload", {"space", "path", "request"}, {"base"}),
}
_POST_FIELDS = {
    "/space": ("space", {"name"}, set()),
    "/device": ("device", {"device"}, set()),
    "/begin": (
        "begin",
        {"space", "path", "size", "checksum", "blocks", "request"},
        {"base"},
    ),
    "/commit": ("commit", {"upload"}, set()),
    "/cancel": ("cancel", {"upload"}, set()),
    "/delete": ("delete", {"file", "base"}, set()),
    "/restore": ("restore", {"file", "revision", "base", "request"}, set()),
    "/share": ("share", {"space", "recipient", "role"}, set()),
    "/ack": ("ack", {"device", "cursor"}, set()),
}


def _duration(value):
    try:
        valid = (
            type(value) in (int, float)
            and math.isfinite(value)
            and 0 < value <= threading.TIMEOUT_MAX
        )
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("invalid socket deadline")
    return value


def _integer(value, maximum=2**63 - 1):
    if (
        not isinstance(value, str)
        or len(value) > 19
        or re.fullmatch(r"[0-9]+", value) is None
    ):
        raise ValueError("invalid integer parameter")
    result = int(value)
    if result > maximum:
        raise ValueError("integer parameter exceeds bound")
    return result


def _pairs(values):
    result = {}
    for name, value in values:
        if name in result:
            raise ValueError("duplicate JSON field")
        result[name] = value
    return result


def _constant(_value):
    raise ValueError("non-finite JSON number")


def _target(value):
    if len(value) > 2048 or re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ValueError("invalid request target")
    target = urlsplit(value)
    if target.scheme or target.netloc or target.fragment:
        raise ValueError("origin-form request target required")
    query = parse_qs(
        target.query,
        keep_blank_values=True,
        max_num_fields=8,
        strict_parsing=True,
        errors="strict",
    )
    if any(len(values) != 1 for values in query.values()):
        raise ValueError("duplicate query parameter")
    return target.path, {name: values[0] for name, values in query.items()}


class Server(ThreadingHTTPServer):
    """Bound all handlers and reserve foreground capacity from long polls.

    Closing cancels admitted long polls, then joins handlers and their deadline
    timers before the owner closes Drive. Socket cancellation cannot undo an
    already accepted mutation or interrupt arbitrary database/storage code.
    """

    daemon_threads = False
    request_queue_size = 64

    def __init__(
        self, *args, concurrency=32, poll_concurrency=None, request_timeout=30
    ):
        if type(concurrency) is not int or not 1 <= concurrency <= 256:
            raise ValueError("invalid HTTP concurrency")
        if poll_concurrency is None:
            poll_concurrency = max(0, concurrency - max(1, concurrency // 4))
        if type(poll_concurrency) is not int or not 0 <= poll_concurrency < concurrency:
            raise ValueError("long polls must reserve foreground capacity")
        self.slots = threading.BoundedSemaphore(concurrency)
        self.polls = threading.BoundedSemaphore(poll_concurrency)
        self.request_timeout = _duration(request_timeout)
        self.stopping = threading.Event()
        self._active = set()
        self._active_lock = threading.Lock()
        self._requests = threading.local()
        super().__init__(*args)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(min(5, self.request_timeout))
        return connection, address

    def process_request(self, request, address):
        if not self.slots.acquire(False):
            try:
                request.settimeout(0.1)
                request.sendall(
                    b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n"
                    b"Cache-Control: private, no-store\r\nConnection: close\r\n\r\n"
                )
                request.shutdown(socket.SHUT_WR)
                request.recv(8192)
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
        cancelled = threading.Event()
        with self._active_lock:
            self._active.add(cancelled)
            if self.stopping.is_set():
                cancelled.set()

        def interrupt():
            cancelled.set()
            with suppress(OSError):
                request.shutdown(socket.SHUT_RDWR)

        timer = threading.Timer(self.request_timeout, interrupt)
        timer.daemon = True
        self._requests.value = cancelled
        timer.start()
        try:
            super().process_request_thread(request, address)
        finally:
            timer.cancel()
            timer.join()
            with self._active_lock:
                self._active.discard(cancelled)
            del self._requests.value
            self.slots.release()

    def server_close(self):
        self.stopping.set()
        with self._active_lock:
            for cancelled in self._active:
                cancelled.set()
        super().server_close()

    def handle_error(self, request, address):
        cancelled = getattr(self._requests, "value", None)
        if (
            isinstance(sys.exc_info()[1], OSError)
            and cancelled is not None
            and cancelled.is_set()
        ):
            return  # Expected transport interruption while parsing headers.
        super().handle_error(request, address)


class _Handler(BaseHTTPRequestHandler):
    """Parse transport once, then delegate to small authenticated route adapters."""

    def setup(self):
        super().setup()
        self.cancelled = self.server._requests.value
        self.deadline = time.monotonic() + self.server.request_timeout
        self.drive = self.server.drive

    def log_message(self, *_):
        pass

    def _live(self):
        if self.cancelled.is_set() or time.monotonic() >= self.deadline:
            raise TimeoutError("request socket deadline expired")
        self.connection.settimeout(max(0.001, min(5, self.deadline - time.monotonic())))

    def _header(self, name):
        values = self.headers.get_all(name, [])
        if len(values) > 1:
            raise ValueError("duplicate header")
        return values[0] if values else None

    def _length(self):
        value = self._header("Content-Length")
        return None if value is None else _integer(value)

    def token(self):
        value = self._header("Authorization")
        if not isinstance(value, str) or not value.startswith("Bearer "):
            raise PermissionError("credential required")
        token = value[7:]
        self.drive.auth(
            token
        )  # Authenticate before accepting a potentially large body.
        return token

    def send(self, status, value, binary=False):
        self._live()
        data = value if binary else json.dumps(value, allow_nan=False).encode()
        self.response_started = True
        self.send_response(status)
        self.send_header(
            "Content-Type", "application/octet-stream" if binary else "application/json"
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def body(self, limit):
        size = self._length()
        if size is None or not 0 <= size <= limit:
            raise ValueError("invalid body length")
        self._live()
        body = self.rfile.read(size)
        if len(body) != size:
            raise ValueError("truncated body")
        self._live()
        return body

    def json_body(self):
        result = json.loads(
            self.body(_JSON_LIMIT), object_pairs_hook=_pairs, parse_constant=_constant
        )
        if not isinstance(result, dict):
            raise ValueError("JSON object required")
        return result

    def config(self, token, _query):
        return self.send(200, {"block_size": self.drive.block_size})

    def upload_status(self, token, query):
        return self.send(200, self.drive.upload_status(token, query["upload"]))

    def metadata(self, token, query):
        return self.send(
            200, self.drive.metadata(token, query["file"], query.get("revision"))
        )

    def files(self, token, query):
        return self.send(200, self.drive.list_files(token, query["space"]))

    def revisions(self, token, query):
        if "limit" not in query:
            return self.send(200, self.drive.revisions(token, query["file"]))
        limit = _integer(query["limit"], 1000)
        if limit < 1:
            raise ValueError("invalid revision limit")
        return self.send(200, self.drive.revisions(token, query["file"], limit=limit))

    def download_block(self, token, query):
        return self.send(
            200,
            self.drive.get_block(
                token, query["file"], query["revision"], _integer(query["index"], 10239)
            ),
            True,
        )

    def upload_block(self, token, query):
        return self.send(
            200,
            {
                "created": self.drive.put(
                    token,
                    query["upload"],
                    query["digest"],
                    self.body(self.drive.block_size),
                )
            },
        )

    def simple_upload(self, token, query):
        return self.send(
            200,
            self.drive.upload(
                token,
                query["space"],
                query["path"],
                self.body(self.drive.block_size),
                base=query.get("base"),
                request=query["request"],
            ),
        )

    def changes(self, token, query):
        cursor = _integer(query.get("cursor", "0"))
        limit = _integer(query.get("limit", "100"), 1000)
        raw_wait = query.get("wait", "0")
        if not raw_wait.isascii() or len(raw_wait) > 30:
            raise ValueError("invalid wait")
        wait = float(raw_wait)
        if not math.isfinite(wait) or not 0 <= wait <= 25 or limit < 1:
            raise ValueError("invalid polling bounds")
        admitted = wait > 0
        if admitted and not self.server.polls.acquire(False):
            return self.send(503, {"error": "notification capacity exhausted; retry"})
        try:
            value = self.drive.changes(
                token, query["device"], cursor, wait, limit, cancel=self.cancelled
            )
            return self.send(200, value)
        finally:
            if admitted:
                self.server.polls.release()

    def post(self, token, name, value):
        drive = self.drive
        if name == "space":
            return self.send(201, {"space": drive.create_space(token, value["name"])})
        if name == "device":
            return self.send(200, drive.device(token, value["device"]))
        if name == "begin":
            return self.send(201, drive.begin(token, **value))
        if name == "commit":
            return self.send(200, drive.commit(token, value["upload"]))
        if name == "cancel":
            drive.cancel(token, value["upload"])
        elif name == "delete":
            return self.send(200, {"revision": drive.delete(token, **value)})
        elif name == "restore":
            return self.send(200, drive.restore(token, **value))
        elif name == "share":
            drive.share(token, **value)
        elif name == "ack":
            drive.ack(token, **value)
        return self.send(200, {})

    def dispatch(self):
        if self._header("Transfer-Encoding") is not None:
            raise ValueError("transfer encoding unsupported")
        self._length()
        self._header("Content-Type")
        path, query = _target(self.path)
        token = self.token()
        if self.command == "POST" and path in _POST_FIELDS:
            if query:
                raise ValueError("unexpected query parameter")
            name, required, optional = _POST_FIELDS[path]
            value = self.json_body()
            if not required <= set(value) or set(value) - required - optional:
                raise ValueError("invalid JSON fields")
            return self.post(token, name, value)
        route = _ROUTES.get((self.command, path))
        if route is None:
            return self.send(404, {"error": "not found"})
        handler, required, optional = route
        if (
            not required <= set(query)
            or set(query) - required - optional
            or any(not v for v in query.values())
        ):
            raise ValueError("invalid query fields")
        if self.command == "GET" and self._length() not in (None, 0):
            raise ValueError("GET body unsupported")
        return getattr(self, handler)(token, query)

    def _error(self, status, message):
        if not getattr(self, "response_started", False):
            with suppress(OSError):
                self.send(status, {"error": message})

    def handle_method(self):
        self.response_started = False
        try:
            self.dispatch()
        except PermissionError:
            self._error(403, "permission denied")
        except Conflict:
            self._error(409, "conflict")
        except ArchivedBlock:
            self._error(423, "restore archived block first")
        except (ValueError, KeyError, TypeError, RecursionError):
            self._error(400, "invalid input or state")
        except sqlite3.Error:
            self._error(503, "metadata unavailable")
        except OSError:
            self._error(503, "storage unavailable")

    do_GET = do_POST = do_PUT = handle_method


def server(
    drive,
    host="127.0.0.1",
    port=0,
    *,
    concurrency=32,
    poll_concurrency=None,
    request_timeout=30,
):
    """Bind an unstarted server; caller owns shutdown/close before Drive.close."""
    http = Server(
        (host, port),
        _Handler,
        concurrency=concurrency,
        poll_concurrency=poll_concurrency,
        request_timeout=request_timeout,
    )
    http.drive = drive
    return http
