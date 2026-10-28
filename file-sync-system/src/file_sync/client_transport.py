"""Owned direct HTTP transport for device requests; redirects never forward credentials."""

from contextlib import suppress
import http.client
import json
import socket
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit


def request(
    url,
    token,
    method,
    route,
    value=None,
    binary=False,
    *,
    block_size=4 * 1024**2,
    timeout=30,
):
    """Use one owned connection and absolute socket budget; never redirect credentials."""
    body = (
        value if binary else json.dumps(value).encode() if value is not None else None
    )
    parsed = urlsplit(url)
    connection_type = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_type(parsed.hostname, parsed.port, timeout=timeout)
    response = None
    state_lock = threading.Lock()
    deadline = time.monotonic() + timeout

    def interrupt():
        with state_lock:
            active = connection.sock
            if response is not None and response.fp is not None:
                active = response.fp.raw._sock
            if active is not None:
                with suppress(OSError):
                    active.shutdown(socket.SHUT_RDWR)

    timer = threading.Timer(timeout, interrupt)
    timer.daemon = True
    timer.start()
    try:
        connection.connect()
        if time.monotonic() >= deadline:
            raise TimeoutError("request deadline exceeded")
        connection.request(
            method,
            parsed.path.rstrip("/") + route,
            body=body,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/octet-stream"
                if binary
                else "application/json",
            },
        )
        received = connection.getresponse()
        with state_lock:
            response = received
        limit = block_size if binary and method == "GET" else 4 * 1024**2
        data = bytearray()
        while len(data) <= limit:
            if response.fp is None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("request deadline exceeded")
            response.fp.raw._sock.settimeout(remaining)
            chunk = response.read1(min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > limit:
            raise OSError("response exceeded bound")
        if not 200 <= response.status < 300:
            raise HTTPError(
                url + route,
                response.status,
                response.reason,
                response.headers,
                None,
            )
        if time.monotonic() >= deadline:
            raise TimeoutError("request deadline exceeded")
        return bytes(data) if binary else json.loads(data)
    finally:
        timer.cancel()
        timer.join()
        if response is not None:
            response.close()
        connection.close()
