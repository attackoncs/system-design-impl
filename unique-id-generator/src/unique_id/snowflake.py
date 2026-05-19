"""Core Snowflake ID generator."""

from __future__ import annotations

import threading
from typing import Optional

from unique_id.clock import Clock, SystemClock
from unique_id.config import SnowflakeConfig
from unique_id.exceptions import ClockMovedBackwardsError


class SnowflakeGenerator:
    """Core Snowflake ID generator.

    Generates 64-bit unique, time-sortable IDs using the Twitter Snowflake
    algorithm. Thread-safe via a threading lock protecting the sequence
    number and last timestamp state.
    """

    def __init__(
        self,
        config: Optional[SnowflakeConfig] = None,
        clock: Optional[Clock] = None,
    ):
        """Initialize the Snowflake generator.

        Args:
            config: Generator configuration. Uses defaults if None.
            clock: Clock implementation. Uses SystemClock if None.
        """
        self._config = config or SnowflakeConfig()
        self._clock = clock or SystemClock()
        self._sequence = 0
        self._last_timestamp = -1
        self._lock = threading.Lock()

    @property
    def config(self) -> SnowflakeConfig:
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
            timestamp = self._current_millis()

            if timestamp < self._last_timestamp:
                raise ClockMovedBackwardsError(self._last_timestamp, timestamp)

            if timestamp == self._last_timestamp:
                self._sequence = (self._sequence + 1) & self._config.max_sequence
                if self._sequence == 0:
                    # Sequence overflow — wait for next millisecond
                    timestamp = self._wait_next_millis(self._last_timestamp)
            else:
                self._sequence = 0

            self._last_timestamp = timestamp

            # Compose the 64-bit ID
            ts_offset = timestamp - self._config.epoch_ms
            id_value = (
                (ts_offset << self._config.timestamp_shift)
                | (self._config.datacenter_id << self._config.datacenter_shift)
                | (self._config.machine_id << self._config.machine_shift)
                | self._sequence
            )
            return id_value

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
        if count <= 0:
            raise ValueError(f"count must be positive, got {count}")

        ids: list[int] = []
        with self._lock:
            for _ in range(count):
                timestamp = self._current_millis()

                if timestamp < self._last_timestamp:
                    raise ClockMovedBackwardsError(self._last_timestamp, timestamp)

                if timestamp == self._last_timestamp:
                    self._sequence = (self._sequence + 1) & self._config.max_sequence
                    if self._sequence == 0:
                        timestamp = self._wait_next_millis(self._last_timestamp)
                else:
                    self._sequence = 0

                self._last_timestamp = timestamp

                ts_offset = timestamp - self._config.epoch_ms
                id_value = (
                    (ts_offset << self._config.timestamp_shift)
                    | (self._config.datacenter_id << self._config.datacenter_shift)
                    | (self._config.machine_id << self._config.machine_shift)
                    | self._sequence
                )
                ids.append(id_value)

        return ids

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
