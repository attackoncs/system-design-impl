"""Hash function implementations for the consistent hash ring.

Provides a protocol defining the hash function interface and several
concrete implementations (SHA-1, MD5, SHA-256) that map string keys
to integer positions on the hash ring.
"""

import hashlib
from typing import Protocol


class HashFunction(Protocol):
    """Protocol for hash functions used by the ring.

    Any callable that accepts a string key and returns an integer
    hash value satisfies this protocol.
    """

    def __call__(self, key: str) -> int:
        """Hash a string key to an integer position on the ring."""
        ...


def sha1_hash(key: str) -> int:
    """Default hash function using SHA-1, returning full 160-bit integer.

    Args:
        key: The string key to hash.

    Returns:
        An integer in the range [0, 2^160 - 1].
    """
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return int(digest, 16)


def md5_hash(key: str) -> int:
    """MD5-based hash function, returning 128-bit integer.

    Args:
        key: The string key to hash.

    Returns:
        An integer in the range [0, 2^128 - 1].
    """
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return int(digest, 16)


def sha256_hash(key: str) -> int:
    """SHA-256-based hash function, returning 256-bit integer.

    Args:
        key: The string key to hash.

    Returns:
        An integer in the range [0, 2^256 - 1].
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest, 16)
