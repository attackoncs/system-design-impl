"""Clock abstraction for millisecond time tracking."""

import time
from abc import ABC, abstractmethod


class Clock(ABC):
    """Abstract clock interface for millisecond time.

    Allows injection of different clock implementations for testing
    and for choosing between system clock and monotonic clock.
    """

    @abstractmethod
    def current_millis(self) -> int:
        """Return current time in milliseconds.

        Returns:
            Current timestamp in milliseconds since Unix epoch.
        """
        ...


class SystemClock(Clock):
    """System clock using time.time().

    Uses the system wall clock. Subject to NTP adjustments and
    clock drift. Suitable for most production use cases where
    NTP is properly configured.
    """

    def current_millis(self) -> int:
        """Return current system time in milliseconds."""
        return int(time.time() * 1000)


class MonotonicClock(Clock):
    """Monotonic clock using time.monotonic().

    Uses a monotonic clock that cannot go backwards. Anchored to
    system time at initialization. Suitable for environments where
    clock drift is a concern.

    Note: Monotonic clock is relative — it's anchored to system time
    at the moment of creation to produce absolute millisecond timestamps.
    """

    def __init__(self) -> None:
        """Initialize by anchoring monotonic clock to current system time."""
        self._anchor_system_ms = int(time.time() * 1000)
        self._anchor_monotonic_ms = int(time.monotonic() * 1000)

    def current_millis(self) -> int:
        """Return current time in milliseconds (monotonically increasing).

        Computes: anchor_system_ms + (current_monotonic - anchor_monotonic)
        """
        elapsed = int(time.monotonic() * 1000) - self._anchor_monotonic_ms
        return self._anchor_system_ms + elapsed
