"""Custom exceptions for the unique ID generator."""


class UniqueIDError(Exception):
    """Base exception for unique ID generator errors."""

    pass


class ClockMovedBackwardsError(UniqueIDError):
    """Raised when the system clock moves backwards.

    This indicates potential NTP adjustment or clock drift.
    The generator refuses to generate IDs to prevent duplicates.

    Covers: FR-4.2
    """

    def __init__(self, last_timestamp: int, current_timestamp: int):
        self.last_timestamp = last_timestamp
        self.current_timestamp = current_timestamp
        self.drift_ms = last_timestamp - current_timestamp
        super().__init__(
            f"Clock moved backwards by {self.drift_ms}ms. "
            f"Last timestamp: {last_timestamp}, current: {current_timestamp}. "
            f"Refusing to generate ID to avoid duplicates."
        )


class SequenceOverflowError(UniqueIDError):
    """Raised when sequence number overflows within a millisecond.

    This should not normally be raised externally — the generator handles
    overflow by waiting for the next millisecond. This is used internally
    or when wait behavior is disabled.

    Covers: FR-4.3
    """

    def __init__(self, timestamp: int, max_sequence: int):
        self.timestamp = timestamp
        self.max_sequence = max_sequence
        super().__init__(
            f"Sequence overflow at timestamp {timestamp}. "
            f"Max sequence: {max_sequence}. "
            f"All {max_sequence + 1} IDs exhausted for this millisecond."
        )


class InvalidConfigError(UniqueIDError):
    """Raised when configuration is invalid.

    Covers: FR-2.4, FR-2.5
    """

    def __init__(self, message: str):
        super().__init__(f"Invalid configuration: {message}")
