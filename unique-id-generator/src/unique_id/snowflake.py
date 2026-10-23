"""Core Snowflake ID generator."""

from __future__ import annotations

import threading
from typing import Optional

import unique_id.clock
import unique_id.config
import unique_id.exceptions


class SnowflakeGenerator:
    """Core Snowflake ID generator.

    Generates 64-bit unique, time-sortable IDs using the Twitter Snowflake
    algorithm. Thread-safe via a threading lock protecting the sequence number
    and last timestamp state.
    """

    def __init__(
        self,
        config: Optional[unique_id.config.SnowflakeConfig] = None,
        clock: Optional[unique_id.clock.Clock] = None,
    ):
        """Initialize the Snowflake generator.

        Args:
            config: Generator configuration. Uses defaults if None.
            clock: Clock implementation. Uses SystemClock if None.
        """
        self._config = config or unique_id.config.SnowflakeConfig()
        self._clock = clock or unique_id.clock.SystemClock()
        self._sequence = 0
        self._last_timestamp = -1
        self._lock = threading.Lock()

    @property
    def config(self) -> unique_id.config.SnowflakeConfig:
        """Get the generator configuration."""
        return self._config

    def generate(self) -> int:
        """Generate a single unique Snowflake ID.

        Thread-safe. Acquires lock, gets current timestamp, manages
        sequence number, and composes the 64-bit ID.

        Returns:
            A 64-bit unique integer ID.

        Raises:
            ClockMovedBackwardsError: If system clock moved backwards.
        """
        with self._lock:
            return self._generate_locked()

    def _generate_locked(self) -> int:
        """Validate first, then atomically publish the next sequence state."""
        timestamp = self._current_millis()
        if timestamp < self._last_timestamp:
            raise unique_id.exceptions.ClockMovedBackwardsError(
                self._last_timestamp, timestamp
            )
        sequence = 0
        if timestamp == self._last_timestamp:
            sequence = (self._sequence + 1) & self._config.max_sequence
            if sequence == 0:
                timestamp = self._wait_next_millis(self._last_timestamp)
        offset = timestamp - self._config.epoch_ms
        if not 0 <= offset <= self._config.max_timestamp:
            raise ValueError("timestamp outside configured Snowflake bit range")
        self._last_timestamp, self._sequence = timestamp, sequence
        return (
            (offset << self._config.timestamp_shift)
            | (self._config.datacenter_id << self._config.datacenter_shift)
            | (self._config.machine_id << self._config.machine_shift)
            | sequence
        )

    def generate_batch(self, count: int) -> list[int]:
        """Generate multiple unique IDs efficiently.

        Acquires the lock once for the entire batch, reducing lock
        acquisition overhead compared to calling generate() in a loop.

        Args:
            count: Number of IDs to generate. Must be positive.

        Returns:
            List of unique integer IDs in strictly increasing order.

        Raises:
            ValueError: If count is not positive.
            ClockMovedBackwardsError: If system clock moved backwards.
        """
        if type(count) is not int or count <= 0:
            raise ValueError(f"count must be positive, got {count}")

        with self._lock:
            return [self._generate_locked() for _ in range(count)]

    def _wait_next_millis(self, last_timestamp: int) -> int:
        """Spin-wait until the clock advances past the given timestamp.

        Called when sequence overflows within a millisecond.

        Args:
            last_timestamp: The timestamp to wait past.

        Returns:
            The new (advanced) timestamp in milliseconds.
        """
        timestamp = self._current_millis()
        while timestamp <= last_timestamp:
            timestamp = self._current_millis()
        return timestamp

    def _current_millis(self) -> int:
        """Get current time in milliseconds from the configured clock."""
        return self._clock.current_millis()
