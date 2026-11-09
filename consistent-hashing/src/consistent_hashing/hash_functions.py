"""Hash function implementations for the consistent hash ring.

Provides a protocol defining the hash function interface and several concrete
implementations (SHA-1, MD5, SHA-256) that map string keys to integer positions
on the hash ring.
"""

import hashlib
from typing import Callable, Protocol


def hash_position(function: Callable[[str], int], key: str) -> int:
    """Validate the shared ring/key contract, including custom callables."""
    if not isinstance(key, str):
        raise ValueError("key must be a string")
    value = function(key)
    if type(value) is not int:
        raise ValueError("hash function must return an integer")
    return value


class HashFunction(Protocol):
    """Protocol for hash functions used by the ring.

    Any callable that accepts a string key and returns an integer hash value
    satisfies this protocol.
    """

    def __call__(self, key: str) -> int:
        """Hash a string key to an integer position on the ring."""


def _digest_int(algorithm: Callable, key: str) -> int:
    """Interpret the hex digest of ``key`` as an unsigned ring position."""
    return int(algorithm(key.encode("utf-8")).hexdigest(), 16)


def sha1_hash(key: str) -> int:
    """Default hash: SHA-1, an integer in [0, 2^160 - 1]."""
    return _digest_int(hashlib.sha1, key)


def md5_hash(key: str) -> int:
    """MD5 hash, an integer in [0, 2^128 - 1]."""
    return _digest_int(hashlib.md5, key)


def sha256_hash(key: str) -> int:
    """SHA-256 hash, an integer in [0, 2^256 - 1]."""
    return _digest_int(hashlib.sha256, key)
