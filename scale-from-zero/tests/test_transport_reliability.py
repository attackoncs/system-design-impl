from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os, socket, sys, threading, time, unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from scale_from_zero.transport import request as rpc


def http_json(url, token=None, **kwargs):
    return rpc(url, token=token, **kwargs)


CONFIG = {"read_token": "a" * 48}


@contextmanager
def running(instance):
    thread = threading.Thread(
        target=instance.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join()


def raw_request(instance, raw):
    with socket.create_connection(instance.server_address, timeout=2) as sock:
        sock.sendall(raw)
        sock.shutdown(socket.SHUT_WR)
        result = bytearray()
        while True:
            part = sock.recv(65536)
            if not part:
                return bytes(result)
            result.extend(part)


class TransportReliabilityTests(unittest.TestCase):
    def peer(self, action):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                try:
                    action(self)
                except OSError:
                    pass

        return ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    def test_proxy_is_bypassed_and_redirect_exposed(self):
        seen = []

        def reply(handler):
            seen.append(handler.path)
            handler.send_response(302)
            handler.send_header("Location", "/elsewhere")
            handler.send_header("Content-Length", "2")
            handler.end_headers()
            handler.wfile.write(b"{}")

        with (
            running(self.peer(reply)) as peer,
            patch.dict(
                os.environ,
                {
                    "http_proxy": "http://127.0.0.1:1",
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "no_proxy": "",
                    "NO_PROXY": "",
                },
            ),
        ):
            with self.assertRaises(HTTPError) as caught:
                http_json(f"http://127.0.0.1:{peer.server_port}/", CONFIG["read_token"])
            self.assertEqual(caught.exception.code, 302)
            self.assertEqual(caught.exception.read(), b"{}")
            caught.exception.close()
        self.assertEqual(seen, ["/"])

    def test_drip_cannot_extend_one_deadline(self):
        def reply(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", "100")
            handler.end_headers()
            for _ in range(20):
                handler.wfile.write(b" ")
                handler.wfile.flush()
                time.sleep(0.04)

        with running(self.peer(reply)) as peer:
            started = time.monotonic()
            with self.assertRaises(OSError):
                http_json(
                    f"http://127.0.0.1:{peer.server_port}/",
                    CONFIG["read_token"],
                    timeout=0.15,
                )
            self.assertLess(time.monotonic() - started, 0.5)

    def test_cancel_interrupts_read(self):
        entered = threading.Event()

        def reply(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", "2")
            handler.end_headers()
            entered.set()
            time.sleep(0.4)
            handler.wfile.write(b"{}")

        stop, errors = threading.Event(), []
        with running(self.peer(reply)) as peer:

            def call():
                try:
                    http_json(
                        f"http://127.0.0.1:{peer.server_port}/",
                        CONFIG["read_token"],
                        cancel=stop,
                    )
                except OSError as error:
                    errors.append(error)

            thread = threading.Thread(target=call)
            thread.start()
            self.assertTrue(entered.wait(1))
            stop.set()
            thread.join(0.3)
            self.assertFalse(thread.is_alive())
        self.assertTrue(errors)

    def test_peer_framing_lengths_and_json_are_checked(self):
        for headers, body in (
            ([("Content-Length", "abc")], b"{}"),
            ([("Content-Length", "2"), ("Content-Length", "2")], b"{}"),
            ([("Content-Length", "5")], b"{}"),
            ([("Content-Length", "13")], b'{"a":1,"a":2}'),
        ):

            def reply(handler):
                handler.send_response(200)
                for key, value in headers:
                    handler.send_header(key, value)
                handler.end_headers()
                handler.wfile.write(body)
                handler.close_connection = True

            with running(self.peer(reply)) as peer:
                with self.assertRaises(OSError):
                    http_json(
                        f"http://127.0.0.1:{peer.server_port}/", CONFIG["read_token"]
                    )
