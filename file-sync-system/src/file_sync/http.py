"""Bounded authenticated HTTP transport, including durable long polling."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import threading
from urllib.parse import parse_qs, urlsplit

from .service import Conflict
from .storage import ArchivedBlock


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, *args):
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(*args)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(30)
        return connection, address

    def process_request(self, request, address):
        if not self.slots.acquire(False):
            try:
                request.sendall(
                    b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n"
                )
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


def server(drive, host="127.0.0.1", port=0):
    class Handler(BaseHTTPRequestHandler):
        # Apply before parsing headers, including clients that send no request.
        timeout = 10.0

        def log_message(self, *_):
            pass

        def send(self, status, value, binary=False):
            data = value if binary else json.dumps(value).encode()
            self.response_started = True
            self.send_response(status)
            self.send_header(
                "Content-Type",
                "application/octet-stream" if binary else "application/json",
            )
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, no-store")
            self.end_headers()
            self.wfile.write(data)

        def body(self, limit):
            length = int(self.headers.get("Content-Length", "-1"))
            if not 0 <= length <= limit:
                raise ValueError("invalid body length")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("truncated body")
            return body

        def dispatch(self):
            if len(self.path) > 2048:
                raise ValueError("target too long")
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                raise PermissionError()
            token = auth[7:]
            target = urlsplit(self.path)
            query = parse_qs(target.query, keep_blank_values=True, max_num_fields=8)
            if any(len(value) != 1 for value in query.values()):
                raise ValueError("duplicate query parameter")

            def get(name, default=None):
                return query[name][0] if name in query else default

            if self.command == "PUT" and target.path == "/block":
                return self.send(
                    200,
                    {
                        "created": drive.put(
                            token,
                            get("upload"),
                            get("digest"),
                            self.body(drive.block_size),
                        )
                    },
                )
            if self.command == "GET":
                if target.path == "/config":
                    drive.auth(token)
                    return self.send(200, {"block_size": drive.block_size})
                if target.path == "/upload":
                    return self.send(200, drive.upload_status(token, get("upload")))
                if target.path == "/metadata":
                    return self.send(
                        200, drive.metadata(token, get("file"), get("revision"))
                    )
                if target.path == "/files":
                    return self.send(200, drive.list_files(token, get("space")))
                if target.path == "/revisions":
                    return self.send(200, drive.revisions(token, get("file")))
                if target.path == "/block":
                    return self.send(
                        200,
                        drive.get_block(
                            token, get("file"), get("revision"), int(get("index"))
                        ),
                        True,
                    )
                if target.path == "/changes":
                    return self.send(
                        200,
                        drive.changes(
                            token,
                            get("device"),
                            int(get("cursor", "0")),
                            float(get("wait", "0")),
                            int(get("limit", "100")),
                        ),
                    )
            if self.command == "POST":
                value = json.loads(self.body(2 * 1024**2))
                if not isinstance(value, dict):
                    raise ValueError("JSON object required")
                if target.path == "/space":
                    return self.send(
                        201, {"space": drive.create_space(token, value["name"])}
                    )
                if target.path == "/device":
                    return self.send(200, drive.device(token, value["device"]))
                if target.path == "/begin":
                    return self.send(201, drive.begin(token, **value))
                if target.path == "/commit":
                    return self.send(200, drive.commit(token, value["upload"]))
                if target.path == "/cancel":
                    drive.cancel(token, value["upload"])
                    return self.send(200, {})
                if target.path == "/delete":
                    return self.send(200, {"revision": drive.delete(token, **value)})
                if target.path == "/restore":
                    return self.send(200, drive.restore(token, **value))
                if target.path == "/share":
                    drive.share(token, **value)
                    return self.send(200, {})
                if target.path == "/ack":
                    drive.ack(token, **value)
                    return self.send(200, {})
            return self.send(404, {"error": "not found"})

        def handle_method(self):
            try:
                self.dispatch()
            except PermissionError:
                self.send(403, {"error": "permission denied"})
            except Conflict:
                self.send(409, {"error": "conflict"})
            except ArchivedBlock:
                self.send(423, {"error": "restore archived block first"})
            except (ValueError, KeyError, TypeError):
                self.send(400, {"error": "invalid input or state"})
            except sqlite3.Error:
                self.send(503, {"error": "metadata unavailable"})
            except OSError:
                if not getattr(self, "response_started", False):
                    self.send(503, {"error": "storage unavailable"})

        do_GET = do_POST = do_PUT = handle_method

    return Server((host, port), Handler)
