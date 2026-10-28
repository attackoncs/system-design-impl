"""Stable SHA256 user routing; topology changes require migration."""

import hashlib
from .common import identifier, integer


def partition(user, count):
    integer(count, 1, 2)
    return int(hashlib.sha256(identifier(user).encode()).hexdigest(), 16) % count
