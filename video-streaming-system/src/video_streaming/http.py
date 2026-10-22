"""Authenticated bounded API and encrypted HLS delivery with byte-range
support.
"""

import http.server
import json
import posixpath
import re
import sqlite3
import threading
import urllib.parse


def playlist_links(data, resource, grant):
    """Rewrite playlist URIs into authenticated media URLs using grant."""

    def link(uri):
        name = posixpath.normpath(
            posixpath.join(posixpath.dirname(resource), uri)
        )
        return "/media?" + urllib.parse.urlencode(
            {"grant": grant, "resource": name}
        )

    lines = []
    for line in data.decode("utf-8").splitlines():
        if line and not line.startswith("#"):
            line = link(line)
        else:
            line = re.sub(
                r'URI="([^"]+)"',
                lambda match: 'URI="' + link(match[1]) + '"',
                line,
            )
        lines.append(line)
    return ("\n".join(lines) + "\n").encode()


def byte_range(value, size):
    """Parse a single HTTP byte range into inclusive offsets.

    Args:
        value: Range header, or a false value to request the entire object.
        size: Object size in bytes.

    Returns:
        A tuple of (first byte, last byte, is partial response).

    Raises:
        ValueError: The header is malformed or its range is unsatisfiable.
    """
    if not value:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
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


class BoundedServer(http.server.ThreadingHTTPServer):
    """A threaded media server with 32 request slots and socket deadlines."""

    daemon_threads = True
    request_queue_size = 128

    def __init__(self, *args):
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(*args)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length:"
                    b" 0\r\nConnection: close\r\n\r\n"
                )
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def server(service, host="127.0.0.1", port=0):
    """Bind an authenticated video HTTP server without starting its loop.

    Args:
        service: VideoService providing upload, status, and media operations.
        host: Listen address, defaulting to loopback.
        port: TCP port, or zero for an available port.

    Returns:
        The bound server; its caller owns serving, shutdown, and server_close.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        """An HTTP request handler bound to the enclosing service instance."""

        def send_response(self, *args, **kwargs):
            self.response_started = True
            return super().send_response(*args, **kwargs)

        def log_message(self, *_):
            pass

        def respond(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def body(self, maximum):
            size = int(self.headers.get("Content-Length", "-1"))
            if not 0 <= size <= maximum:
                raise ValueError("body size invalid")
            data = self.rfile.read(size)
            if len(data) != size:
                raise ValueError("body incomplete")
            return data

        def token(self):
            value = self.headers.get("Authorization", "")
            if not value.startswith("Bearer "):
                raise PermissionError("credential required")
            return value[7:]

        def dispatch(self):
            if len(self.path) > 4096:
                raise ValueError("target too long")
            target = urllib.parse.urlsplit(self.path)
            params = urllib.parse.parse_qs(
                target.query, keep_blank_values=True, max_num_fields=8
            )
            if any(len(value) != 1 for value in params.values()):
                raise ValueError("duplicate parameter")

            def param(key):
                return params[key][0]

            if self.command == "GET" and target.path == "/media":
                grant, resource = param("grant"), param("resource")
                path = service.asset(grant, resource)
                size = path.stat().st_size
                if resource.endswith(".m3u8"):
                    data = playlist_links(
                        service.read_small(path), resource, grant
                    )
                    self.send_response(200)
                    self.send_header(
                        "Content-Type", "application/vnd.apple.mpegurl"
                    )
                    self.send_header("Cache-Control", "private, no-store")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                try:
                    first, last, partial = byte_range(
                        self.headers.get("Range"), size
                    )
                except ValueError:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(206 if partial else 200)
                self.send_header(
                    "Content-Type",
                    (
                        "image/png"
                        if resource.endswith(".png")
                        else "application/octet-stream"
                    ),
                )
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(last - first + 1))
                if partial:
                    self.send_header(
                        "Content-Range", f"bytes {first}-{last}/{size}"
                    )
                self.end_headers()
                if size <= min(1024**2, service.cache_bytes):
                    self.wfile.write(service.read_small(path)[first : last + 1])
                else:
                    with path.open("rb") as stream:
                        stream.seek(first)
                        remaining = last - first + 1
                        while remaining:
                            chunk = stream.read(min(65536, remaining))
                            if not chunk:
                                raise OSError("asset truncated")
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                return
            if self.command == "PUT" and target.path == "/part":
                grant = param("grant")
                claims = service.verify(grant, "upload")
                body = self.body(claims["size"])
                return self.respond(
                    200,
                    {
                        "created": service.put_part(
                            grant, body, self.headers.get("X-Checksum-SHA256")
                        )
                    },
                )
            if self.command == "POST" and target.path == "/uploads":
                value = json.loads(self.body(65536))
                if not isinstance(value, dict):
                    raise ValueError("object body required")
                return self.respond(
                    201,
                    service.create(
                        self.token(),
                        value["title"],
                        value["size"],
                        value.get("part_size", 4 * 1024**2),
                    ),
                )
            if self.command == "POST" and target.path == "/finish":
                return self.respond(
                    200,
                    {
                        "video_id": service.finish(
                            self.token(),
                            param("video"),
                            self.headers.get("X-Checksum-SHA256"),
                        )
                    },
                )
            if self.command == "POST" and target.path == "/renew":
                return self.respond(
                    200, service.upload_grants(self.token(), param("video"))
                )
            if self.command == "POST" and target.path == "/playback":
                return self.respond(
                    200,
                    {"grant": service.playback(self.token(), param("video"))},
                )
            if self.command == "POST" and target.path == "/remove":
                service.remove(self.token(), param("video"))
                return self.respond(200, {"removed": True})
            if self.command == "GET" and target.path == "/videos":
                return self.respond(
                    200, service.status(self.token(), param("video"))
                )
            self.respond(404, {"error": "not found"})

        def handle_method(self):
            try:
                self.dispatch()
            except PermissionError:
                self.respond(
                    403,
                    {"error": "invalid credential/grant or video unavailable"},
                )
            except (ValueError, KeyError, TypeError):
                self.respond(400, {"error": "invalid request or video state"})
            except sqlite3.Error:
                self.respond(503, {"error": "metadata unavailable"})
            except OSError:
                # Includes broken connections and temporary object
                # unavailability.
                if not getattr(self, "response_started", False):
                    self.respond(503, {"error": "object unavailable"})

        do_GET = do_POST = do_PUT = handle_method

    return BoundedServer((host, port), Handler)
