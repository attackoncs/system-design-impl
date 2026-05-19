"""Shared test helpers."""

from unique_id.clock import Clock


class FakeClock(Clock):
    """A controllable clock for deterministic testing.

    Allows setting and advancing the time manually.
    """

    def __init__(self, start_ms: int = 1704067200000):
        """Initialize with a starting time in milliseconds.

        Args:
            start_ms: Starting time in milliseconds (default: 2024-01-01).
        """
        self._current_ms = start_ms

    def current_millis(self) -> int:
        """Return the current controlled time."""
        return self._current_ms

    def set_millis(self, ms: int) -> None:
        """Set the current time to a specific value.

        Args:
            ms: Time in milliseconds to set.
        """
        self._current_ms = ms

    def advance(self, ms: int) -> None:
        """Advance the clock by a given number of milliseconds.

        Args:
            ms: Number of milliseconds to advance.
        """
        self._current_ms += ms
