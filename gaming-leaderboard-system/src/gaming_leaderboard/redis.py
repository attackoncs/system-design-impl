"""One-command RESP2 connections with bounded framing and shared deadlines."""

import math
import socket
import time

from .redis_protocol import Reader, RedisError as RedisError, encode
from .transport import Deadline, interrupt_socket


class Redis:
    def __init__(self, host, port, password):
        if (
            not isinstance(host, str)
            or not host
            or type(port) is not int
            or not 1 <= port <= 65535
        ):
            raise ValueError("invalid Redis endpoint")
        if not isinstance(password, str) or not password:
            raise ValueError("Redis password required")
        self.host, self.port, self.password = host, port, password

    encode = staticmethod(encode)

    @staticmethod
    def read(stream):
        return Reader(stream).read()

    def command(self, *args, timeout=3, deadline=None, cancel=None):
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("positive finite Redis timeout required")
        if deadline is not None and (
            type(deadline) not in (int, float) or not math.isfinite(deadline)
        ):
            raise ValueError("finite Redis deadline required")
        end = (
            min(time.monotonic() + timeout, deadline)
            if deadline is not None
            else time.monotonic() + timeout
        )
        remaining = end - time.monotonic()
        if remaining <= 0 or (cancel is not None and cancel.is_set()):
            raise TimeoutError("Redis command cancelled or deadline exceeded")
        payload, auth = self.encode(args), self.encode(["AUTH", self.password])
        # Connect is separately bounded; DNS is outside socket cancellation.
        with socket.create_connection(
            (self.host, self.port), timeout=min(0.3, remaining)
        ) as connection:
            with Deadline(end, lambda: interrupt_socket(connection), cancel):
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                connection.settimeout(max(0.001, end - time.monotonic()))
                with connection.makefile("rb") as stream:
                    if time.monotonic() >= end or (
                        cancel is not None and cancel.is_set()
                    ):
                        raise TimeoutError(
                            "Redis command cancelled or deadline exceeded"
                        )
                    connection.sendall(auth)
                    if self.read(stream) != "OK":
                        raise RedisError("invalid Redis authentication acknowledgement")
                    connection.sendall(payload)
                    result = self.read(stream)
                    if time.monotonic() >= end or (
                        cancel is not None and cancel.is_set()
                    ):
                        raise TimeoutError(
                            "Redis command cancelled or deadline exceeded"
                        )
                    return result
