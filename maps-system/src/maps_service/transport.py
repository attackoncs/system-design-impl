"""Direct bounded HTTP transport with owned cancellation and no redirects.

Socket deadlines do not forcibly interrupt platform DNS or arbitrary CPU/SQL work.
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

from .store import dumps

MAX_CATALOG = 64 * 1024 * 1024
MAX_BODY = 2 * 1024 * 1024
MAX_PUBLIC = 16 * 1024 * 1024


def integer(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}", value):
        raise ValueError("invalid integer")
    return int(value)


def loads(value):
    return json.loads(
        value, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float
    )


def _seconds(value, *, zero=False):
    try:
        valid = (
            type(value) in (int, float)
            and math.isfinite(value)
            and (value >= 0 if zero else value > 0)
            and value <= threading.TIMEOUT_MAX
        )
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("invalid duration")
    return value


def _url(value, *, query=False):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 8192
        or any(ord(c) <= 32 for c in value)
    ):
        raise ValueError("invalid HTTP endpoint")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.port == 0
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.query and not query)
    ):
        raise ValueError("invalid HTTP endpoint")
    return parsed


def _secret(value):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 4096
        and value.isascii()
        and all(ord(c) > 32 for c in value)
    )


def _pairs(values):
    result = {}
    for key, value in values:
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


class _Deadline:
    """Own one short-lived deadline watcher and join it on every exit path."""

    def __init__(self, deadline, interrupt, cancel=None):
        self.deadline, self.interrupt, self.cancel = deadline, interrupt, cancel
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self._watch, name="maps-io-deadline")

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


def fetch(
    url,
    method="GET",
    body=None,
    token=None,
    *,
    timeout=3,
    deadline=None,
    cancel=None,
    max_bytes=MAX_CATALOG,
):
    """Read one bounded response directly, without redirects or proxies.

    Host/SNI and normal HTTPS certificate validation are retained. The deadline
    covers headers and streaming body reads; a cancelled operation keeps its
    caller's capacity slot until the underlying operation actually exits.
    """
    _seconds(timeout)
    if method not in ("GET", "POST"):
        raise ValueError("unsupported upstream method")
    data = dumps(body).encode() if body is not None else None
    if data is not None and len(data) > MAX_BODY:
        raise ValueError("upstream request exceeds local limit")
    parsed = _url(url, query=True)
    if token is not None and not _secret(token):
        raise ValueError("invalid bearer credential")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_CATALOG:
        raise ValueError("invalid response limit")
    deadline = (
        min(time.monotonic() + timeout, deadline)
        if deadline is not None
        else time.monotonic() + timeout
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0 or (cancel is not None and cancel.is_set()):
        raise TimeoutError("HTTP operation expired")
    connection_type = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_type(parsed.hostname, parsed.port, timeout=remaining)
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
        with _Deadline(deadline, interrupt, cancel):
            connection.connect()
            if time.monotonic() >= deadline or (cancel is not None and cancel.is_set()):
                raise TimeoutError("HTTP operation expired")
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            headers = {"Accept": "application/json, application/geo+json, image/png"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            if token is not None:
                headers["Authorization"] = "Bearer " + token
            connection.request(method, target, body=data, headers=headers)
            received = connection.getresponse()
            with state_lock:
                response = received
            if response.status != 200:
                # A redirect is evidence of a different endpoint, never authority
                # to send the replication credential somewhere else.
                raise HTTPError(
                    url,
                    response.status,
                    "upstream rejected request",
                    response.headers,
                    None,
                )
            lengths = response.headers.get_all("Content-Length", [])
            encodings = response.headers.get_all("Transfer-Encoding", [])
            if (
                len(lengths) > 1
                or len(encodings) > 1
                or (lengths and encodings)
                or (encodings and encodings[0].strip().lower() != "chunked")
            ):
                raise ValueError("ambiguous upstream response framing")
            if lengths and integer(lengths[0]) > max_bytes:
                raise ValueError("upstream response exceeds local limit")
            data = bytearray()
            while len(data) <= max_bytes:
                if time.monotonic() >= deadline or (
                    cancel is not None and cancel.is_set()
                ):
                    raise TimeoutError("HTTP operation expired")
                part = response.read1(min(65536, max_bytes + 1 - len(data)))
                if not part:
                    break
                data.extend(part)
            if len(data) > max_bytes:
                raise ValueError("upstream response exceeds local limit")
            # read1() may return EOF before Content-Length is exhausted without
            # raising IncompleteRead. A complete JSON value is not sufficient
            # evidence that its HTTP message arrived intact.
            if response.length not in (None, 0):
                raise ValueError("truncated upstream response")
            if time.monotonic() >= deadline or (cancel is not None and cancel.is_set()):
                raise TimeoutError("HTTP operation expired")
            content_types = response.headers.get_all("Content-Type", [])
            controls = response.headers.get_all("Cache-Control", [])
            if len(content_types) > 1 or len(controls) > 1:
                raise ValueError("ambiguous upstream metadata")
            content_type = content_types[0] if content_types else "application/json"
            control = controls[0] if controls else "no-store"
            return bytes(data), content_type, control
    finally:
        if response is not None:
            response.close()
        connection.close()
