"""Bounded RESP2 framing, independent from socket ownership and retries."""

import math
import re

MAX_BYTES = 8 * 1024 * 1024
MAX_ITEMS = 100_000
MAX_ARRAY = 65_536
MAX_DEPTH = 16
MAX_LINE = 65_536


class RedisError(Exception):
    pass


def encode(args):
    if not args or len(args) > 1024:
        raise ValueError("Redis command requires 1..1024 arguments")
    chunks = [b"*" + str(len(args)).encode() + b"\r\n"]
    total = len(chunks[0])
    for arg in args:
        if isinstance(arg, bytes):
            data = arg
        elif isinstance(arg, str):
            data = arg.encode()
        elif type(arg) is int or (type(arg) is float and math.isfinite(arg)):
            data = str(arg).encode()
        else:
            raise ValueError("Redis arguments must be strings, bytes or finite numbers")
        header = b"$" + str(len(data)).encode() + b"\r\n"
        total += len(header) + len(data) + 2
        if total > MAX_BYTES:
            raise ValueError("Redis command exceeds 8 MiB")
        chunks.extend([header, data, b"\r\n"])
    return b"".join(chunks)


class Reader:
    def __init__(self, stream):
        self.stream, self.bytes, self.items = stream, 0, 0

    def _consume(self, size):
        self.bytes += size
        if self.bytes > MAX_BYTES:
            raise RedisError("RESP response exceeds 8 MiB")

    @staticmethod
    def _number(value):
        if len(value) > 20 or not re.fullmatch(rb"-?(0|[1-9][0-9]*)", value):
            raise RedisError("invalid RESP integer")
        number = int(value)
        if not -(2**63) <= number < 2**63:
            raise RedisError("RESP integer out of range")
        return number

    @staticmethod
    def _text(value):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise RedisError("invalid RESP UTF-8") from None

    def read(self, depth=0):
        self.items += 1
        if depth > MAX_DEPTH or self.items > MAX_ITEMS:
            raise RedisError("RESP nesting or item budget exceeded")
        line = self.stream.readline(min(MAX_LINE + 1, MAX_BYTES - self.bytes + 1))
        self._consume(len(line))
        if not line or len(line) > MAX_LINE or not line.endswith(b"\r\n"):
            raise RedisError("truncated or oversized RESP line")
        marker, value = line[:1], line[1:-2]
        if marker == b"+":
            return self._text(value)
        if marker == b"-":
            raise RedisError(value.decode(errors="replace"))
        if marker == b":":
            return self._number(value)
        if marker not in (b"$", b"*"):
            raise RedisError("unsupported RESP type")
        length = self._number(value)
        if length == -1:
            return None
        if length < 0:
            raise RedisError("invalid RESP length")
        if marker == b"$":
            if length + 2 > MAX_BYTES - self.bytes:
                raise RedisError("RESP response exceeds 8 MiB")
            data = self.stream.read(length + 2)
            self._consume(len(data))
            if len(data) != length + 2 or not data.endswith(b"\r\n"):
                raise RedisError("truncated RESP bulk")
            return self._text(data[:-2])
        if length > MAX_ARRAY or length > MAX_ITEMS - self.items:
            raise RedisError("RESP array exceeds budget")
        return [self.read(depth + 1) for _ in range(length)]
