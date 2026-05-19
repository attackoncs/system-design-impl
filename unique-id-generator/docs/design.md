# Design: Unique ID Generator

## Architecture Overview

The unique ID generator follows a simple layered architecture with a core Snowflake generator, configurable bit layout, clock abstraction for monotonic time tracking, and an ID parser for decomposition. Alternative strategies provide fallback options.

```
┌─────────────────────────────────────────────────────────────────┐
│                      Application Layer                           │
│         (basic_usage.py, multi_generator.py examples)           │
├─────────────────────────────────────────────────────────────────┤
│                      Public API                                  │
│   (SnowflakeGenerator.generate / generate_batch / IDParser)     │
├──────────────────────┬──────────────────────────────────────────┤
│   Core Generator     │       Alternative Strategies             │
│  (Snowflake algo,    │   (UUIDGenerator,                        │
│   sequence mgmt,     │    TimestampRandomGenerator)             │
│   thread safety)     │                                          │
├──────────────────────┴──────────────────────────────────────────┤
│                    Clock Abstraction                             │
│        (SystemClock, MonotonicClock — millisecond time)          │
├─────────────────────────────────────────────────────────────────┤
│                    Configuration                                 │
│   (SnowflakeConfig — bit layout, epoch, datacenter/machine ID)  │
├─────────────────────────────────────────────────────────────────┤
│                    ID Parser                                     │
│   (Decompose ID → timestamp, datacenter, machine, sequence)     │
└─────────────────────────────────────────────────────────────────┘
```

### ID Generation Flow

```
generate() called
        │
        ▼
┌──────────────────┐
│  Acquire Lock    │ ◄── Thread safety (threading.Lock)
└────────┬─────────┘
         │
         ▼
┌──────────────────┐     ┌──────────────────┐
│  Get Current     │────▶│  Clock returns   │
│  Timestamp (ms)  │     │  milliseconds    │
└────────┬─────────┘     └──────────────────┘
         │
         ▼
┌──────────────────────────────────────────┐
│  Compare with last_timestamp             │
│                                          │
│  • ts > last_ts → reset sequence to 0   │
│  • ts == last_ts → increment sequence   │
│  • ts < last_ts → ClockMovedBackwards!  │
└────────┬─────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────┐
│  Check sequence overflow                 │
│                                          │
│  • sequence > max → wait_next_millis()  │
│    then reset sequence to 0              │
└────────┬─────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────┐
│  Compose 64-bit ID:                      │
│                                          │
│  0 | timestamp | datacenter | machine | seq │
│  1    41 bits     5 bits     5 bits   12 bits │
│                                          │
│  id = (ts << 22) | (dc << 17) |         │
│       (machine << 12) | sequence         │
└────────┬─────────────────────────────────┘
         │
         ▼
┌──────────────────┐
│  Release Lock    │
│  Return ID       │
└──────────────────┘
```

## Project Structure

```
unique-id-generator/
├── pyproject.toml
├── README.md
├── docs/
│   ├── requirements.md
│   ├── design.md
│   └── tasks.md
├── src/
│   └── unique_id/
│       ├── __init__.py
│       ├── snowflake.py      # Core Snowflake generator
│       ├── config.py         # Configuration (bit layout, epoch)
│       ├── clock.py          # Clock abstraction (monotonic, NTP-aware)
│       ├── parser.py         # ID parsing/decomposition
│       ├── strategies.py     # Alternative strategies (UUID, timestamp-random)
│       └── exceptions.py     # Custom exceptions
├── tests/
│   ├── __init__.py
│   ├── test_snowflake.py
│   ├── test_config.py
│   ├── test_clock.py
│   ├── test_parser.py
│   ├── test_strategies.py
│   └── test_properties.py
└── examples/
    ├── basic_usage.py
    └── multi_generator.py
```

## Component Design

### 1. Configuration

```python
from dataclasses import dataclass


# Default epoch: 2024-01-01T00:00:00Z in milliseconds
DEFAULT_EPOCH_MS = 1704067200000


@dataclass(frozen=True)
class SnowflakeConfig:
    """Configuration for the Snowflake ID generator.

    Defines the bit layout, custom epoch, and machine identity.
    The total bits (timestamp + datacenter + machine + sequence) must equal 63
    (sign bit is always 0).

    Covers: FR-2.1, FR-2.2, FR-2.3, FR-2.4, FR-2.5, FR-2.6
    """

    # Bit allocation
    timestamp_bits: int = 41    # ~69 years from epoch
    datacenter_bits: int = 5    # 32 datacenters
    machine_bits: int = 5       # 32 machines per datacenter
    sequence_bits: int = 12     # 4096 IDs per millisecond per machine

    # Identity
    datacenter_id: int = 0
    machine_id: int = 0

    # Epoch
    epoch_ms: int = DEFAULT_EPOCH_MS  # 2024-01-01T00:00:00Z

    def __post_init__(self) -> None:
        """Validate configuration on creation.

        Raises:
            InvalidConfigError: If bit allocation is invalid or IDs exceed capacity.
        """
        ...

    @property
    def max_datacenter_id(self) -> int:
        """Maximum datacenter ID for configured bits (2^datacenter_bits - 1)."""
        return (1 << self.datacenter_bits) - 1

    @property
    def max_machine_id(self) -> int:
        """Maximum machine ID for configured bits (2^machine_bits - 1)."""
        return (1 << self.machine_bits) - 1

    @property
    def max_sequence(self) -> int:
        """Maximum sequence number for configured bits (2^sequence_bits - 1)."""
        return (1 << self.sequence_bits) - 1

    @property
    def max_timestamp(self) -> int:
        """Maximum timestamp value for configured bits (2^timestamp_bits - 1)."""
        return (1 << self.timestamp_bits) - 1

    @property
    def timestamp_shift(self) -> int:
        """Number of bits to left-shift the timestamp."""
        return self.datacenter_bits + self.machine_bits + self.sequence_bits

    @property
    def datacenter_shift(self) -> int:
        """Number of bits to left-shift the datacenter ID."""
        return self.machine_bits + self.sequence_bits

    @property
    def machine_shift(self) -> int:
        """Number of bits to left-shift the machine ID."""
        return self.sequence_bits
```

### 2. Exceptions

```python
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
```

### 3. Clock Abstraction

```python
from abc import ABC, abstractmethod
import time


class Clock(ABC):
    """Abstract clock interface for millisecond time.

    Allows injection of different clock implementations for testing
    and for choosing between system clock and monotonic clock.

    Covers: FR-4.4
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
```

### 4. Snowflake Generator

```python
import threading
from typing import Optional


class SnowflakeGenerator:
    """Core Snowflake ID generator.

    Generates 64-bit unique, time-sortable IDs using the Twitter Snowflake
    algorithm. Thread-safe via a threading lock protecting the sequence
    number and last timestamp state.

    Covers: FR-1, FR-3, FR-4, FR-5, FR-7
    """

    def __init__(
        self,
        config: Optional["SnowflakeConfig"] = None,
        clock: Optional["Clock"] = None,
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
    def config(self) -> "SnowflakeConfig":
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
```

### 5. ID Parser

```python
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class ParsedID:
    """A decomposed Snowflake ID with all extracted components.

    Covers: FR-6.4
    """
    id_value: int
    timestamp_ms: int       # Milliseconds since epoch
    datacenter_id: int
    machine_id: int
    sequence: int
    datetime_utc: datetime  # Converted to UTC datetime

    def __repr__(self) -> str:
        return (
            f"ParsedID(id={self.id_value}, "
            f"timestamp_ms={self.timestamp_ms}, "
            f"datacenter={self.datacenter_id}, "
            f"machine={self.machine_id}, "
            f"sequence={self.sequence}, "
            f"datetime={self.datetime_utc.isoformat()})"
        )


class IDParser:
    """Parses Snowflake IDs back into their constituent components.

    Uses the same configuration (bit layout, epoch) as the generator
    that created the ID to correctly extract fields.

    Covers: FR-6.1, FR-6.2, FR-6.3
    """

    def __init__(self, config: Optional["SnowflakeConfig"] = None):
        """Initialize the parser with a configuration.

        Args:
            config: Configuration matching the generator. Uses defaults if None.
        """
        self._config = config or SnowflakeConfig()

    def parse(self, id_value: int) -> ParsedID:
        """Parse a Snowflake ID into its components.

        Extracts timestamp, datacenter ID, machine ID, and sequence number
        by applying bit masks and shifts based on the configuration.

        Args:
            id_value: The 64-bit Snowflake ID to parse.

        Returns:
            ParsedID with all extracted fields.

        Raises:
            ValueError: If id_value is negative or exceeds 64 bits.
        """
        if id_value < 0:
            raise ValueError(f"ID must be non-negative, got {id_value}")
        if id_value.bit_length() > 63:
            raise ValueError(f"ID exceeds 63 bits: {id_value}")

        config = self._config

        # Extract sequence (lowest bits)
        sequence_mask = (1 << config.sequence_bits) - 1
        sequence = id_value & sequence_mask

        # Extract machine ID
        machine_mask = (1 << config.machine_bits) - 1
        machine_id = (id_value >> config.machine_shift) & machine_mask

        # Extract datacenter ID
        datacenter_mask = (1 << config.datacenter_bits) - 1
        datacenter_id = (id_value >> config.datacenter_shift) & datacenter_mask

        # Extract timestamp offset
        timestamp_mask = (1 << config.timestamp_bits) - 1
        ts_offset = (id_value >> config.timestamp_shift) & timestamp_mask

        # Convert to absolute timestamp
        timestamp_ms = ts_offset + config.epoch_ms

        # Convert to datetime
        datetime_utc = datetime.fromtimestamp(
            timestamp_ms / 1000.0, tz=timezone.utc
        )

        return ParsedID(
            id_value=id_value,
            timestamp_ms=timestamp_ms,
            datacenter_id=datacenter_id,
            machine_id=machine_id,
            sequence=sequence,
            datetime_utc=datetime_utc,
        )
```

### 6. Alternative Strategies

```python
import uuid
import time
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass


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
```

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Language & Runtime | Python >= 3.9 | Modern Python features (type hints, dataclasses); consistent with other projects in repo |
| Build System | hatchling (pyproject.toml) | Modern Python packaging standard, consistent with other projects in repo |
| Project Layout | src layout (`src/unique_id/`) | Prevents accidental imports from project root; packaging best practice |
| Thread Safety | `threading.Lock` | Simple, effective for single-process use; GIL already provides some safety but lock ensures correctness |
| Clock Abstraction | Interface-based (ABC) | Allows injection of mock clocks for testing; supports monotonic clock for drift-prone environments |
| Configuration | Frozen dataclass | Immutable after creation prevents accidental mutation; `__post_init__` validates on construction |
| Bit Layout | Configurable with defaults | Default matches Twitter Snowflake (41+5+5+12) but allows tuning for different scale requirements |
| Sequence Overflow | Wait-for-next-ms (spin) | Ensures no ID loss; acceptable latency spike for rare overflow scenario (>4096 IDs in 1ms) |
| Clock Drift | Raise exception | Fail-fast prevents duplicate IDs; caller can implement retry/backoff strategy |
| ID Type | Plain `int` | 64-bit Python int; no string conversion overhead; directly usable as database primary key |
| Batch Generation | Single lock acquisition | Reduces overhead for bulk ID generation; maintains ordering guarantees |
| Alternative Strategies | Separate classes, common interface | Clean separation; easy comparison; fallback options for different requirements |
| Epoch | 2024-01-01T00:00:00Z | Recent epoch maximizes usable timestamp range (~69 years from 2024) |
| Parser | Separate class | Single Responsibility; can be used independently of generator; supports forensic analysis of IDs |

## Error Handling

| Error Scenario | Handling Strategy |
|----------------|-------------------|
| **Clock moved backwards** | Raise `ClockMovedBackwardsError` with drift duration. Caller should wait or use monotonic clock. Generator refuses to produce IDs to prevent duplicates. |
| **Sequence overflow** | Spin-wait until next millisecond (`_wait_next_millis`). Transparent to caller. Only occurs at >4096 IDs/ms/machine with default config. |
| **Invalid configuration** | Raise `InvalidConfigError` at construction time. Fail-fast with descriptive message (which constraint was violated). |
| **Datacenter/machine ID too large** | Raise `InvalidConfigError` during config validation. Message includes max allowed value for configured bits. |
| **Bit allocation doesn't sum to 63** | Raise `InvalidConfigError` during config validation. Message shows actual sum vs expected 63. |
| **Negative or oversized ID for parsing** | Raise `ValueError` from `IDParser.parse()`. Validates input before attempting extraction. |
| **Batch count <= 0** | Raise `ValueError` from `generate_batch()`. Must be a positive integer. |
| **Timestamp exceeds max** | Would occur after ~69 years from epoch. Raise `OverflowError` with message suggesting epoch reconfiguration. |
