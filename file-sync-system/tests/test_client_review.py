"""A parseable body is not sufficient evidence of a complete HTTP response."""

import contextlib
import http.server
import json
import threading

import pytest

from file_sync.client import DeviceClient
from file_sync.client_transport import request


@contextlib.contextmanager
def serving(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()


class QuietHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass


@pytest.mark.parametrize("binary", [False, True])
def test_truncated_content_length_is_rejected_for_json_and_blocks(binary):
    body = b"data" if binary else b'{"ready":true}'

    class Handler(QuietHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body) + 1))
            self.end_headers()
            self.wfile.write(body)

    with serving(Handler) as url:
        with pytest.raises(OSError, match="incomplete"):
            request(url, "owner", "GET", "/", binary=binary)


@pytest.mark.parametrize("content_length", [False, True])
def test_complete_and_close_delimited_json_responses_are_accepted(content_length):
    body = b'{"ready":true}'

    class Handler(QuietHandler):
        def do_GET(self):
            self.send_response(200)
            if content_length:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with serving(Handler) as url:
        assert request(url, "owner", "GET", "/") == {"ready": True}


def test_truncated_chunked_response_is_a_retryable_io_error():
    body = b'{"ready":true}'

    class Handler(QuietHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n")
            # EOF without the required zero-size terminating chunk.

    with serving(Handler) as url:
        with pytest.raises(OSError, match="incomplete"):
            request(url, "owner", "GET", "/")


def test_truncated_changes_do_not_advance_cursor_and_complete_retry_succeeds(tmp_path):
    truncated = [True]

    class Handler(QuietHandler):
        def do_GET(self):
            value = (
                {"block_size": 4}
                if self.path == "/config"
                else {"events": [], "cursor": 5}
            )
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header(
                "Content-Length",
                str(
                    len(body)
                    + (1 if self.path.startswith("/changes?") and truncated[0] else 0)
                ),
            )
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with serving(Handler) as url:
        client = DeviceClient(url, "owner", tmp_path / "device", "one")
        try:
            with pytest.raises(OSError, match="incomplete"):
                client.pull()
            assert (
                client.db.execute(
                    "SELECT value FROM state WHERE key='cursor'"
                ).fetchone()[0]
                == "0"
            )
            truncated[0] = False
            assert client.pull() == {"events": [], "cursor": 5}
            assert (
                client.db.execute(
                    "SELECT value FROM state WHERE key='cursor'"
                ).fetchone()[0]
                == "5"
            )
        finally:
            client.close()
