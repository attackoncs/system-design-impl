"""Direct JSON RPC with one I/O deadline and no proxy or redirect forwarding.

DNS, application CPU work and SQLite operations cannot be forcibly interrupted
by a socket deadline. Callers retain their capacity until accepted work exits.
"""

from contextlib import suppress
import http.client
import json
import math
import re
import socket
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit

from .common import dumps

MAX_BODY = 32 * 1024 * 1024


def seconds(value):
    try:
        valid = (
            type(value) in (int, float) and math.isfinite(value) and 0 < value <= 3600
        )
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("duration must be finite and in (0, 3600]")
    return value


def secret(value):
    return (
        isinstance(value, str)
        and 8 <= len(value) <= 4096
        and value.isascii()
        and all(32 < ord(char) < 127 for char in value)
    )


def endpoint(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 8192
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("invalid HTTP endpoint")
    result = urlsplit(value)
    if (
        result.scheme not in ("http", "https")
        or not result.hostname
        or result.port == 0
        or result.username is not None
        or result.password is not None
        or result.query
        or result.fragment
    ):
        raise ValueError("invalid HTTP endpoint")
    return result


def length(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}", value):
        raise ValueError("invalid HTTP content length")
    return int(value)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("non-finite JSON number")


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def loads(value):
    return json.loads(
        value, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float
    )


class Deadline:
    """Own and join the short-lived watcher that shuts down timed-out sockets."""

    def __init__(self, deadline, interrupt, cancel=None):
        self.deadline, self.interrupt, self.cancel = deadline, interrupt, cancel
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self._watch, name="queue-io-deadline")

    def _watch(self):
        while not self.finished.is_set():
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or (self.cancel is not None and self.cancel.is_set()):
                self.interrupt()
                return
            self.finished.wait(min(0.05, remaining))

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.finished.set()
        self.thread.join()


def request(
    url, path, body, token, *, timeout=1, deadline=None, cancel=None, max_bytes=MAX_BODY
):
    seconds(timeout)
    parsed = endpoint(url)
    if not isinstance(path, str) or not re.fullmatch(r"/[a-z][a-z-]{0,63}", path):
        raise ValueError("invalid RPC path")
    if not secret(token):
        raise ValueError("invalid bearer credential")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BODY:
        raise ValueError("invalid response limit")
    payload = dumps(body).encode()
    if len(payload) > MAX_BODY:
        raise ValueError("RPC request exceeds 32 MiB")
    if deadline is not None:
        try:
            valid = type(deadline) in (int, float) and math.isfinite(deadline)
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("invalid deadline")
    deadline = (
        min(time.monotonic() + timeout, deadline)
        if deadline is not None
        else time.monotonic() + timeout
    )

    def live():
        if time.monotonic() >= deadline or (cancel is not None and cancel.is_set()):
            raise TimeoutError("RPC operation expired")

    live()
    connection_type = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_type(
        parsed.hostname, parsed.port, timeout=deadline - time.monotonic()
    )
    response, state_lock = None, threading.Lock()

    def interrupt():
        with state_lock:
            active_socket = connection.sock
            active_file = response.fp if response is not None else None
            if active_file is not None:
                active_socket = active_file.raw._sock
            if active_socket is not None:
                with suppress(OSError):
                    active_socket.shutdown(socket.SHUT_RDWR)

    try:
        with Deadline(deadline, interrupt, cancel):
            connection.connect()
            live()
            connection.request(
                "POST",
                parsed.path.rstrip("/") + path,
                body=payload,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Connection": "close",
                },
            )
            received = connection.getresponse()
            with state_lock:
                response = received
            if response.status != 200:
                raise HTTPError(
                    url + path,
                    response.status,
                    "RPC peer rejected request",
                    response.headers,
                    None,
                )
            lengths = response.headers.get_all("Content-Length", [])
            transfers = response.headers.get_all("Transfer-Encoding", [])
            if (
                len(lengths) > 1
                or len(transfers) > 1
                or (lengths and transfers)
                or (transfers and transfers[0].strip().lower() != "chunked")
            ):
                raise ValueError("ambiguous RPC response framing")
            if lengths and length(lengths[0]) > max_bytes:
                raise ValueError("RPC response exceeds local limit")
            result = bytearray()
            while len(result) <= max_bytes:
                live()
                part = response.read1(min(65536, max_bytes + 1 - len(result)))
                if not part:
                    break
                result.extend(part)
            if len(result) > max_bytes:
                raise ValueError("RPC response exceeds local limit")
            live()
            if response.length not in (None, 0):
                raise ValueError("truncated RPC response")
            types = response.headers.get_all("Content-Type", [])
            if len(types) > 1 or (
                types
                and types[0].split(";", 1)[0].strip().lower() != "application/json"
            ):
                raise ValueError("JSON RPC response required")
            return loads(bytes(result))
    except http.client.HTTPException:
        # Match urllib's connection-failure family for existing retry/failover
        # callers; never include endpoint credentials or peer-controlled text.
        raise OSError("RPC peer protocol failed") from None
    finally:
        if response is not None:
            response.close()
        connection.close()
