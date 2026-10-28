"""Validated immutable consistent-hash routing configuration."""

from bisect import bisect_left
import hashlib
import re
from types import MappingProxyType
from urllib.parse import parse_qs, urlsplit

from .models import Error, user_id


def redis_url(value):
    """Keep credentials encoded and reject URL overrides of transport budgets."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 8192
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
    ):
        raise Error(400, "invalid Redis URL")
    try:
        parts = urlsplit(value)
        query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
        databases = query.get("db", [])
        if (
            parts.scheme not in ("redis", "rediss")
            or not parts.hostname
            or parts.port == 0
            or parts.fragment
            or not re.fullmatch(r"/(?:[0-9]{1,10})?", parts.path or "/")
            or set(query) - {"db"}
            or len(databases) > 1
            or (databases and re.fullmatch(r"[0-9]{1,10}", databases[0]) is None)
        ):
            raise ValueError()
        for db in (parts.path.lstrip("/"), *databases):
            if db and int(db) >= 2**31:
                raise ValueError()
    except (ValueError, TypeError):
        raise Error(400, "invalid Redis URL") from None
    return value


class Ring:
    def __init__(self, nodes):
        if not isinstance(nodes, dict) or not 1 <= len(nodes) <= 32:
            raise Error(400, "ring requires 1..32 broker ID/URL pairs")
        nodes = dict(nodes)
        owned = {user_id(node): redis_url(url) for node, url in nodes.items()}
        self.nodes = MappingProxyType(owned)
        self.points = tuple(
            sorted(
                (self.hash(f"{node}:{i}"), node) for node in owned for i in range(64)
            )
        )
        self.tokens = tuple(point[0] for point in self.points)

    @staticmethod
    def hash(value):
        return int.from_bytes(hashlib.sha256(value.encode()).digest(), "big")

    def owner(self, user):
        user_id(user)
        return self.points[
            bisect_left(self.tokens, self.hash(user)) % len(self.points)
        ][1]
