"""Hash strategies for short code generation.

Provides base-62 encoding/decoding utilities, the abstract HashStrategy
interface, and the IDGenerator protocol for pluggable ID generation.
"""

import struct
import zlib
from abc import ABC, abstractmethod
from typing import Protocol

from url_shortener.storage import StorageBackend
from url_shortener.exceptions import CollisionLimitExceededError

# Base-62 character set: 0-9, a-z, A-Z
BASE62_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
SHORT_CODE_LENGTH = 7


class HashStrategy(ABC):
    """Abstract interface for short code generation strategies."""

    @abstractmethod
    def generate(self, long_url: str, storage: StorageBackend) -> str:
        """Generate a short code for the given long URL.

        Args:
            long_url: The URL to shorten.
            storage: Storage backend for collision checking.

        Returns:
            A 7-character short code from the base-62 alphabet.
        """
        ...


class IDGenerator(Protocol):
    """Protocol for unique numeric ID generation."""

    def next_id(self) -> int:
        """Return the next unique numeric ID."""
        ...


def encode_base62(number: int) -> str:
    """Encode a non-negative integer to a base-62 string.

    Args:
        number: Non-negative integer to encode.

    Returns:
        Base-62 encoded string (variable length).
    """
    if number == 0:
        return BASE62_CHARS[0]

    result = []
    while number > 0:
        number, remainder = divmod(number, 62)
        result.append(BASE62_CHARS[remainder])
    return "".join(reversed(result))


def decode_base62(encoded: str) -> int:
    """Decode a base-62 string back to an integer.

    Args:
        encoded: Base-62 encoded string.

    Returns:
        The decoded non-negative integer.

    Raises:
        ValueError: If the string contains invalid characters.
    """
    result = 0
    for char in encoded:
        index = BASE62_CHARS.find(char)
        if index == -1:
            raise ValueError(f"Invalid base-62 character: {char}")
        result = result * 62 + index
    return result


def hash_crc32(text: str) -> int:
    """Compute CRC32 hash of a string, returning a signed 32-bit integer."""
    return zlib.crc32(text.encode("utf-8"))


class HashCollisionStrategy(HashStrategy):
    """Hash-based strategy with collision resolution.

    Applies CRC32 to the long URL, encodes the result in base-62,
    takes the first 7 characters. On collision, appends a predefined
    string and rehashes.
    """

    DEFAULT_MAX_RETRIES = 10
    COLLISION_SUFFIX = "~rehash"

    def __init__(self, max_retries: int = DEFAULT_MAX_RETRIES) -> None:
        self._max_retries = max_retries

    def generate(self, long_url: str, storage: StorageBackend) -> str:
        """Generate short code with collision resolution.

        Args:
            long_url: The URL to shorten.
            storage: Storage backend for collision checking.

        Returns:
            A unique 7-character short code.

        Raises:
            CollisionLimitExceededError: If max retries exceeded.
        """
        url_to_hash = long_url

        for attempt in range(self._max_retries + 1):
            candidate = self._compute_code(url_to_hash)

            # Check if this code is free or already maps to our URL
            existing = storage.get_by_short_code(candidate)
            if existing is None or existing.long_url == long_url:
                return candidate

            # Collision: append suffix and retry
            url_to_hash = url_to_hash + self.COLLISION_SUFFIX

        raise CollisionLimitExceededError(
            f"Failed to generate unique short code for '{long_url}' "
            f"after {self._max_retries} retries"
        )

    def _compute_code(self, text: str) -> str:
        """Compute a 7-character base-62 code from text using CRC32."""
        # CRC32 hash; ensure unsigned 32-bit interpretation via struct
        crc_value = hash_crc32(text)
        # Convert to unsigned 32-bit: pack as signed, unpack as unsigned
        # In Python 3, zlib.crc32 already returns unsigned, but we handle
        # both cases for robustness using bitwise mask
        crc_unsigned = crc_value & 0xFFFFFFFF
        # Encode to base-62 and pad/truncate to 7 characters
        encoded = encode_base62(crc_unsigned)
        return encoded[:SHORT_CODE_LENGTH].ljust(SHORT_CODE_LENGTH, "0")


class AutoIncrementIDGenerator:
    """Default auto-incrementing ID generator.

    Thread-unsafe; suitable for single-threaded usage and testing.
    """

    def __init__(self, start: int = 1) -> None:
        self._counter = start

    def next_id(self) -> int:
        """Return the next sequential ID."""
        current = self._counter
        self._counter += 1
        return current


class Base62Strategy(HashStrategy):
    """Base-62 conversion strategy using unique numeric IDs.

    Converts a unique ID to base-62, producing a guaranteed-unique
    short code without collision handling.
    """

    def __init__(self, id_generator: IDGenerator | None = None) -> None:
        self._id_generator = id_generator or AutoIncrementIDGenerator()

    def generate(self, long_url: str, storage: StorageBackend) -> str:
        """Generate short code from a unique numeric ID.

        Args:
            long_url: The URL to shorten (used for dedup check only).
            storage: Storage backend (used for dedup check only).

        Returns:
            A 7-character base-62 short code.
        """
        unique_id = self._id_generator.next_id()
        encoded = encode_base62(unique_id)
        # Pad to 7 characters with leading zeros
        return encoded.rjust(SHORT_CODE_LENGTH, "0")
