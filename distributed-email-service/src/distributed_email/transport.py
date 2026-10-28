"""Direct JSON RPC with one I/O deadline and no proxy or redirect forwarding.

DNS, application CPU work and SQLite operations cannot be forcibly interrupted
by a socket deadline. Callers retain their capacity until accepted work exits.
"""

from contextlib import suppress
import _socket
import http.client
from io import BytesIO
import json
import math
import re
import socket
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit

from .common import dumps

MAX_BODY = 20 * 1024 * 1024
MAX_REQUEST = 16 * 1024 * 1024


def interrupt_socket(active_socket):
    """Invalidate an owned descriptor, including outstanding makefile reads.

    On Windows shutdown() does not wake Python's timeout-mode select(), while
    socket.close() defers the OS close as long as a makefile references it.
    The stdlib socket base closes and invalidates the descriptor immediately;
    final cleanup remains idempotent and cannot close a subsequently reused fd.
    """
    if active_socket is None:
        return
    with suppress(OSError):
        active_socket.shutdown(socket.SHUT_RDWR)
    with suppress(OSError):
        _socket.socket.close(active_socket)


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
        self.thread = threading.Thread(target=self._watch, name="email-io-deadline")

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
    url,
    method="GET",
    body=None,
    token=None,
    headers=None,
    timeout=5,
    *,
    deadline=None,
    cancel=None,
    max_bytes=MAX_BODY,
):
    seconds(timeout)
    parsed = endpoint(url)
    if method not in ("GET", "POST", "PUT", "DELETE"):
        raise ValueError("unsupported HTTP method")
    if token is not None and not secret(token):
        raise ValueError("invalid bearer credential")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BODY:
        raise ValueError("invalid response limit")
    payload = dumps(body).encode() if body is not None else None
    if payload is not None and len(payload) > MAX_REQUEST:
        raise ValueError("RPC request exceeds 16 MiB")
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
                interrupt_socket(active_socket)

    try:
        with Deadline(deadline, interrupt, cancel):
            connection.connect()
            live()
            outgoing = dict(headers or {})
            if any(
                key.lower()
                in {"host", "content-length", "transfer-encoding", "connection"}
                for key in outgoing
            ):
                raise ValueError("reserved HTTP header")
            if token is not None:
                outgoing = {
                    key: value
                    for key, value in outgoing.items()
                    if key.lower() != "authorization"
                }
                outgoing["Authorization"] = "Bearer " + token
            if payload is not None:
                outgoing["Content-Type"] = "application/json"
            outgoing["Accept"], outgoing["Connection"] = "application/json", "close"
            target = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
            connection.request(method, target, body=payload, headers=outgoing)
            received = connection.getresponse()
            with state_lock:
                response = received
            lengths = response.headers.get_all("Content-Length", [])
            transfers = response.headers.get_all("Transfer-Encoding", [])
            if (
                len(lengths) > 1
                or len(transfers) > 1
                or (lengths and transfers)
                or (transfers and transfers[0].strip().lower() != "chunked")
            ):
                raise OSError("ambiguous RPC response framing")
            if lengths:
                try:
                    response_size = length(lengths[0])
                except ValueError:
                    raise OSError("invalid RPC content length") from None
                if response_size > max_bytes:
                    raise OSError("RPC response exceeds local limit")
            result = bytearray()
            while len(result) <= max_bytes:
                live()
                part = response.read1(min(65536, max_bytes + 1 - len(result)))
                if not part:
                    break
                result.extend(part)
            if len(result) > max_bytes:
                raise OSError("RPC response exceeds local limit")
            live()
            if response.length not in (None, 0):
                raise OSError("truncated RPC response")
            if not 200 <= response.status < 300:
                raise HTTPError(
                    url,
                    response.status,
                    "RPC peer rejected request",
                    response.headers,
                    BytesIO(bytes(result[: 1024 * 1024])),
                )
            types = response.headers.get_all("Content-Type", [])
            if len(types) > 1 or (
                types
                and types[0].split(";", 1)[0].strip().lower() != "application/json"
            ):
                raise OSError("JSON RPC response required")
            try:
                return loads(bytes(result))
            except (ValueError, UnicodeError, RecursionError):
                raise OSError("invalid JSON RPC response") from None
    except http.client.HTTPException:
        # Match urllib's connection-failure family for existing retry/failover
        # callers; never include endpoint credentials or peer-controlled text.
        raise OSError("RPC peer protocol failed") from None
    finally:
        if response is not None:
            response.close()
        connection.close()
