"""Alternative ID generation strategies.

Provides UUID v4 and timestamp-random generators as alternatives to the
core Snowflake algorithm, implementing a common IDGenerator interface.
"""

from __future__ import annotations

import random
import time
import uuid
from abc import ABC, abstractmethod


class IDGenerator(ABC):
    """Abstract base for ID generation strategies.

    Provides a common interface for different ID generation approaches.

    Covers: FR-8.3
    """

    @abstractmethod
    def generate(self) -> int | str:
        """Generate a single unique ID.

        Returns:
            A unique identifier (int for numeric strategies, str for UUID).
        """
        ...

    @abstractmethod
    def generate_batch(self, count: int) -> list[int | str]:
        """Generate multiple unique IDs.

        Args:
            count: Number of IDs to generate.

        Returns:
            List of unique identifiers.
        """
        ...


class UUIDGenerator(IDGenerator):
    """UUID v4 generator (128-bit random).

    Trade-offs vs Snowflake:
    - Pro: No coordination needed, globally unique without configuration
    - Pro: No clock dependency
    - Con: 128 bits (not 64-bit), not time-sortable
    - Con: String representation with hyphens (36 chars)
    - Con: Poor database index performance (random distribution)

    Covers: FR-8.1
    """

    def generate(self) -> str:
        """Generate a UUID v4 string."""
        return str(uuid.uuid4())

    def generate_batch(self, count: int) -> list[str]:
        """Generate multiple UUID v4 strings."""
        return [str(uuid.uuid4()) for _ in range(count)]


class TimestampRandomGenerator(IDGenerator):
    """Timestamp + random bits generator (64-bit).

    Combines a millisecond timestamp (42 bits) with random bits (22 bits).
    Simpler than Snowflake but with weaker uniqueness guarantees.

    Trade-offs vs Snowflake:
    - Pro: No machine/datacenter configuration needed
    - Pro: Simpler implementation
    - Con: Probabilistic uniqueness (collision possible within same ms)
    - Con: Not strictly monotonic (random suffix)
    - Con: Cannot extract machine identity from ID

    Covers: FR-8.2
    """

    def __init__(self, epoch_ms: int = 1704067200000):
        """Initialize with a custom epoch.

        Args:
            epoch_ms: Custom epoch in milliseconds (default: 2024-01-01).
        """
        self._epoch_ms = epoch_ms
        self._random = random.SystemRandom()

    def generate(self) -> int:
        """Generate a 64-bit timestamp-random ID."""
        ts = int(time.time() * 1000) - self._epoch_ms
        random_bits = self._random.getrandbits(22)
        return (ts << 22) | random_bits

    def generate_batch(self, count: int) -> list[int]:
        """Generate multiple timestamp-random IDs."""
        return [self.generate() for _ in range(count)]
