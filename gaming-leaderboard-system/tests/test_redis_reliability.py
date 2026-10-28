"""RESP framing and socket fault injection, not a simulated Redis backend."""

from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
import socketserver
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from gaming_leaderboard.redis import Redis, RedisError
from gaming_leaderboard.redis_protocol import MAX_BYTES, Reader


@contextmanager
def peer(action):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                auth = Reader(self.rfile).read()
                if auth != ["AUTH", "password"]:
                    return
                self.wfile.write(b"+OK\r\n")
                Reader(self.rfile).read()
                action(self)
            except (OSError, RedisError):
                pass

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield Redis("127.0.0.1", server.server_address[1], "password")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class RESPFramingTests(unittest.TestCase):
    def test_valid_text_integer_nil_and_nested_array(self):
        self.assertEqual(
            Redis.read(BytesIO(b"*4\r\n+OK\r\n:-2\r\n$-1\r\n*1\r\n$3\r\nabc\r\n")),
            ["OK", -2, None, ["abc"]],
        )

    def test_malformed_numbers_markers_utf8_and_truncation_are_redis_errors(self):
        for raw in (
            b":+1\r\n",
            b":9223372036854775808\r\n",
            b"*-2\r\n",
            b"?1\r\n",
            b"$3\r\na\r\n",
            b"+\xff\r\n",
            b"+bad\n",
            b"$x\r\n",
        ):
            with self.subTest(raw=raw), self.assertRaises(RedisError):
                Redis.read(BytesIO(raw))

    def test_nesting_and_array_declarations_are_bounded_before_allocation(self):
        for raw in (b"*65537\r\n", b"*1\r\n" * 18 + b"+OK\r\n"):
            with self.assertRaises(RedisError):
                Redis.read(BytesIO(raw))

    def test_total_bulk_budget_is_shared_across_array(self):
        size = 3 * 1024 * 1024
        raw = (
            b"*3\r\n"
            + (b"$" + str(size).encode() + b"\r\n" + b"a" * size + b"\r\n") * 3
        )
        stream = BytesIO(raw)
        with self.assertRaises(RedisError):
            Redis.read(stream)
        self.assertLess(stream.tell(), MAX_BYTES)

    def test_total_item_budget_is_shared_across_nested_arrays(self):
        raw = b"*2\r\n*65536\r\n" + b":0\r\n" * 65536 + b"*40000\r\n"
        with self.assertRaises(RedisError):
            Redis.read(BytesIO(raw))

    def test_command_encoding_preserves_binary_and_bounds_inputs(self):
        self.assertEqual(
            Redis.encode(["SET", b"\x00\xff", 1]),
            b"*3\r\n$3\r\nSET\r\n$2\r\n\x00\xff\r\n$1\r\n1\r\n",
        )
        for args in ([], ["SET", None], [True], [float("nan")], [b"x" * MAX_BYTES]):
            with self.assertRaises(ValueError):
                Redis.encode(args)


class RedisSocketTests(unittest.TestCase):
    def test_direct_socket_reads_peer_and_surfaces_redis_error(self):
        for response, expected in (
            (b"+PONG\r\n", "PONG"),
            (b"-WRONGTYPE corrupt\r\n", None),
        ):
            with peer(lambda handler: handler.wfile.write(response)) as client:
                if expected is None:
                    with self.assertRaises(RedisError):
                        client.command("PING")
                else:
                    self.assertEqual(client.command("PING"), expected)

    def test_dripping_reply_cannot_extend_total_deadline(self):
        def drip(handler):
            for byte in b"$20\r\nabcdefghijklmnopqrst\r\n":
                handler.wfile.write(bytes([byte]))
                time.sleep(0.02)

        with peer(drip) as client:
            started = time.monotonic()
            with self.assertRaises((OSError, RedisError)):
                client.command("GET", "key", timeout=1, deadline=started + 0.12)
            self.assertLess(time.monotonic() - started, 0.5)

    def test_cancel_interrupts_a_blocked_response(self):
        ready, release, cancel = threading.Event(), threading.Event(), threading.Event()

        def wait(handler):
            ready.set()
            release.wait(2)

        errors = []
        with peer(wait) as client:

            def call():
                try:
                    client.command("GET", "key", timeout=2, cancel=cancel)
                except (OSError, RedisError) as error:
                    errors.append(error)

            thread = threading.Thread(target=call)
            thread.start()
            try:
                self.assertTrue(ready.wait(1))
                cancel.set()
                thread.join(0.5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(errors), 1)
            finally:
                release.set()
                thread.join(3)

    def test_stopped_and_invalid_budgets_never_open_connection(self):
        client = Redis("127.0.0.1", 12345, "password")
        stop = threading.Event()
        stop.set()
        with patch("gaming_leaderboard.redis.socket.create_connection") as connect:
            with self.assertRaises(TimeoutError):
                client.command("PING", cancel=stop)
            for budget in (float("nan"), float("inf"), True, 0):
                with self.assertRaises(ValueError):
                    client.command("PING", timeout=budget)
            with self.assertRaises(ValueError):
                client.command("PING", deadline=float("nan"))
            connect.assert_not_called()
