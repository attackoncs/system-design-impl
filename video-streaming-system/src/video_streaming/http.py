"""Bounded authenticated uploads and encrypted HLS delivery.

Socket deadlines bound header/body/response I/O, not database or storage calls.
The caller owns serving and closes the server before closing its VideoService.
"""

import contextlib
import http.server
import json
import math
import posixpath
import re
import socket
import sqlite3
import threading
import time
import urllib.parse

_MANIFEST_LIMIT = 1024**2
_JSON_LIMIT = 65536
_BODYLESS = frozenset({"/finish", "/renew", "/playback", "/remove"})
_ROUTES = {
    ("GET", "/media"): ("media", {"grant", "resource"}),
    ("HEAD", "/media"): ("media", {"grant", "resource"}),
    ("PUT", "/part"): ("upload_part", {"grant"}),
    ("POST", "/uploads"): ("create_upload", set()),
    ("POST", "/metadata"): ("update_metadata", {"video"}),
    ("POST", "/finish"): ("finish_upload", {"video"}),
    ("POST", "/renew"): ("renew_upload", {"video"}),
    ("POST", "/playback"): ("playback", {"video"}),
    ("POST", "/remove"): ("remove_video", {"video"}),
    ("GET", "/videos"): ("video_status", {"video"}),
}


def playlist_links(data, resource, grant):
    """Rewrite relative playlist URIs into grant-authorized media URLs."""

    def link(uri):
        name = posixpath.normpath(posixpath.join(posixpath.dirname(resource), uri))
        return "/media?" + urllib.parse.urlencode({"grant": grant, "resource": name})

    lines = []
    for line in data.decode("utf-8").splitlines():
        if line and not line.startswith("#"):
            line = link(line)
        else:
            line = re.sub(
                r'URI="([^"]+)"', lambda match: 'URI="' + link(match[1]) + '"', line
            )
        lines.append(line)
    return ("\n".join(lines) + "\n").encode()


def byte_range(value, size):
    """Parse one ASCII byte range into inclusive offsets and a partial flag."""
    if type(size) is not int or size < 0:
        raise ValueError("invalid object size")
    if value is None or value == "":
        return 0, size - 1, False
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError("invalid range")
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value)
    if not match or not any(match.groups()):
        raise ValueError("invalid range")
    first, last = match.groups()
    if first:
        first = int(first)
        last = min(size - 1, int(last)) if last else size - 1
    else:
        count = int(last)
        if count <= 0:
            raise ValueError("invalid suffix range")
        first, last = max(0, size - count), size - 1
    if not 0 <= first <= last < size:
        raise ValueError("unsatisfiable range")
    return first, last, True


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
        raise ValueError("invalid request timeout")
    return value


class BoundedServer(http.server.ThreadingHTTPServer):
    """Bound active handlers and drain them before the owner closes storage.

    One timer per admitted request interrupts slow socket streams at an absolute
    deadline. Database/object operations may finish after that deadline; accepted
    mutations cannot be rolled back merely because a client stops waiting.
    """

    daemon_threads = False
    request_queue_size = 128

    def __init__(self, *args, concurrency=32, request_timeout=30):
        if type(concurrency) is not int or not 1 <= concurrency <= 256:
            raise ValueError("invalid HTTP concurrency")
        self.request_timeout = _duration(request_timeout)
        self.slots = threading.BoundedSemaphore(concurrency)
        super().__init__(*args)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(min(5, self.request_timeout))
        return connection, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(False):
            try:
                request.settimeout(0.1)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                    b"Cache-Control: no-store\r\nConnection: close\r\n\r\n"
                )
                request.shutdown(socket.SHUT_WR)
                # Drain sent bytes to preserve the 503 response on Windows.
                request.recv(8192)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        def interrupt():
            with contextlib.suppress(OSError):
                request.shutdown(socket.SHUT_RDWR)

        timer = threading.Timer(self.request_timeout, interrupt)
        timer.daemon = True
        timer.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            timer.cancel()
            timer.join()
            self.slots.release()


def _parameters(target):
    if len(target) > 4096 or re.search(r"%(?![0-9a-fA-F]{2})", target):
        raise ValueError("invalid request target")
    parsed = urllib.parse.urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ValueError("invalid request target")
    params = urllib.parse.parse_qs(
        parsed.query,
        keep_blank_values=True,
        max_num_fields=8,
        strict_parsing=True,
        errors="strict",
    )
    if any(len(values) != 1 for values in params.values()):
        raise ValueError("duplicate parameter")
    return parsed.path, {key: value[0] for key, value in params.items()}


def _reject_constant(value):
    raise ValueError("nonfinite JSON value: " + value)


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


class _Handler(http.server.BaseHTTPRequestHandler):
    """HTTP adapter; handlers delegate authorization and state to VideoService."""

    @property
    def service(self):
        return self.server.service

    def setup(self):
        self.deadline = time.monotonic() + self.server.request_timeout
        super().setup()

    def log_message(self, *_):
        pass

    def handle(self):
        # Deadline shutdown/client disconnect can interrupt header parsing too.
        with contextlib.suppress(OSError):
            super().handle()

    def _live(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("request deadline expired")

    def send_response(self, *args, **kwargs):
        self.response_started = True
        return super().send_response(*args, **kwargs)

    def _header(self, name):
        values = self.headers.get_all(name, [])
        if len(values) > 1:
            raise ValueError("duplicate " + name)
        return values[0] if values else None

    def _length(self):
        value = self._header("Content-Length")
        if value is None:
            return None
        if len(value) > 10 or not re.fullmatch(r"[0-9]+", value):
            raise ValueError("invalid Content-Length")
        return int(value)

    def _framing(self):
        if self.headers.get_all("Transfer-Encoding"):
            raise ValueError("Transfer-Encoding is not supported")
        self._length()
        self._header("Authorization")
        expectation = self._header("Expect")
        if expectation is not None and expectation.lower() != "100-continue":
            raise ValueError("unsupported expectation")

    def body(self, maximum, *, exact=None):
        size = self._length()
        if (
            size is None
            or not 0 <= size <= maximum
            or (exact is not None and size != exact)
        ):
            raise ValueError("invalid body size")
        if self._header("Expect") is not None:
            self.send_response_only(100)
            self.end_headers()
        data = self.rfile.read(size)
        self._live()
        if len(data) != size:
            raise ValueError("incomplete body")
        return data

    def token(self):
        value = self._header("Authorization") or ""
        if not value.startswith("Bearer ") or not value[7:]:
            raise PermissionError("credential required")
        return value[7:]

    def json_body(self):
        value = json.loads(
            self.body(_JSON_LIMIT),
            parse_constant=_reject_constant,
            object_pairs_hook=_json_object,
        )
        if not isinstance(value, dict):
            raise ValueError("object body required")
        return value

    def respond(self, status, value):
        data = json.dumps(value, allow_nan=False).encode()
        self._live()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def media(self, params):
        grant, resource = params["grant"], params["resource"]
        path = self.service.asset(grant, resource)
        size = path.stat().st_size
        if resource.endswith(".m3u8"):
            return self.manifest(path, resource, grant, size)
        try:
            first, last, partial = byte_range(self._header("Range"), size)
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "private, no-store")
            self.end_headers()
            return
        self._live()
        self.send_response(206 if partial else 200)
        self.send_header(
            "Content-Type",
            {
                ".png": "image/png",
                ".ts": "video/mp2t",
                ".mp4": "video/mp4",
            }.get(posixpath.splitext(resource)[1], "application/octet-stream"),
        )
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(last - first + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
        self.end_headers()
        if self.command != "HEAD":
            self.stream(path, size, first, last)

    def manifest(self, path, resource, grant, size):
        if size > _MANIFEST_LIMIT:
            raise OSError("manifest exceeds serving limit")
        try:
            data = self.service.read_small(path, maximum=_MANIFEST_LIMIT)
        except ValueError:
            raise OSError("manifest exceeds serving limit") from None
        if len(data) > _MANIFEST_LIMIT:
            raise OSError("manifest exceeds serving limit")
        data = playlist_links(data, resource, grant)
        self._live()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def stream(self, path, size, first, last):
        if size <= min(_MANIFEST_LIMIT, self.service.cache_bytes):
            self.wfile.write(self.service.read_small(path)[first : last + 1])
            return
        with path.open("rb") as stream:
            stream.seek(first)
            remaining = last - first + 1
            while remaining:
                self._live()
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    raise OSError("asset truncated")
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def upload_part(self, params):
        grant = params["grant"]
        claims = self.service.verify(grant, "upload")
        size = claims["size"]
        if type(size) is not int or not 1 <= size <= 8 * 1024**2:
            raise PermissionError("invalid upload grant")
        data = self.body(size, exact=size)
        self.respond(
            200,
            {
                "created": self.service.put_part(
                    grant, data, self._header("X-Checksum-SHA256")
                )
            },
        )

    def create_upload(self, params):
        token = self.token()
        self.service.authenticate(token)
        value = self.json_body()
        if set(value) - {"title", "size", "part_size"}:
            raise ValueError("unknown upload field")
        self.respond(
            201,
            self.service.create(
                token,
                value["title"],
                value["size"],
                value.get("part_size", 4 * 1024**2),
            ),
        )

    def update_metadata(self, params):
        token = self.token()
        self.service.authenticate(token)
        value = self.json_body()
        if set(value) != {"title"}:
            raise ValueError("title required")
        self.respond(
            200, self.service.update_metadata(token, params["video"], value["title"])
        )

    def finish_upload(self, params):
        self.respond(
            200,
            {
                "video_id": self.service.finish(
                    self.token(), params["video"], self._header("X-Checksum-SHA256")
                )
            },
        )

    def renew_upload(self, params):
        self.respond(200, self.service.upload_grants(self.token(), params["video"]))

    def playback(self, params):
        self.respond(
            200, {"grant": self.service.playback(self.token(), params["video"])}
        )

    def remove_video(self, params):
        self.service.remove(self.token(), params["video"])
        self.respond(200, {"removed": True})

    def video_status(self, params):
        self.respond(200, self.service.status(self.token(), params["video"]))

    def dispatch(self):
        self._framing()
        path, params = _parameters(self.path)
        route = _ROUTES.get((self.command, path))
        if route is None:
            return self.respond(404, {"error": "not found"})
        handler, required = route
        if set(params) != required or any(not value for value in params.values()):
            raise ValueError("invalid parameters")
        if path in _BODYLESS and self._length() not in (None, 0):
            raise ValueError("unexpected request body")
        self._live()
        return getattr(self, handler)(params)

    def handle_method(self):
        self.response_started = False
        try:
            self.dispatch()
        except PermissionError:
            self._error(403, "invalid credential/grant or video unavailable")
        except (ValueError, KeyError, TypeError, RecursionError):
            self._error(400, "invalid request or video state")
        except sqlite3.Error:
            self._error(503, "metadata unavailable")
        except OSError:
            self._error(503, "object unavailable")

    def _error(self, status, message):
        if not self.response_started:
            with contextlib.suppress(OSError):
                self.respond(status, {"error": message})

    do_GET = do_HEAD = do_POST = do_PUT = handle_method


def server(service, host="127.0.0.1", port=0, *, concurrency=32, request_timeout=30):
    """Bind an unstarted server; close drains handlers before storage is closed."""
    bound = BoundedServer(
        (host, port), _Handler, concurrency=concurrency, request_timeout=request_timeout
    )
    bound.service = service
    return bound
