# Design: Unique ID Generator

> 2026-10-31：[开源对照、章节覆盖与122项验证](open-source-review_cn.md)；[历史审查](chapter08-review.md)。组件实现以源码链接为准，避免维护第二份过期代码。

## Architecture Overview

The unique ID generator follows a simple layered architecture with a core Snowflake generator, configurable bit layout, clock abstraction for monotonic time tracking, and an ID parser for decomposition. Alternative strategies are explicit comparison options, not automatic failover.

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

[SnowflakeConfig](../src/unique_id/config.py)

### 2. Exceptions

[Exceptions](../src/unique_id/exceptions.py)

### 3. Clock Abstraction

[Clock](../src/unique_id/clock.py)

### 4. Snowflake Generator

[SnowflakeGenerator](../src/unique_id/snowflake.py)

### 5. ID Parser

[IDParser](../src/unique_id/parser.py)

### 6. Alternative Strategies

[UUIDGenerator / TimestampRandomGenerator](../src/unique_id/strategies.py)

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
| Sequence Overflow | Bounded wait with sleep | Monotonic deadline (default 1s); timeout raises SequenceOverflowError without resetting used state |
| Clock Drift | Raise exception | Fail-fast prevents duplicate IDs; caller can implement retry/backoff strategy |
| ID Type | Plain `int` | Python int constrained to 0..2**63-1 for Snowflake; suitable for signed 64-bit columns |
| Batch Generation | Single lock acquisition | Reduces overhead for bulk ID generation; maintains ordering guarantees |
| Alternative Strategies | Separate classes, common interface | Clean separation; easy comparison; fallback options for different requirements |
| Epoch | 2024-01-01T00:00:00Z | Recent epoch maximizes usable timestamp range (~69 years from 2024) |
| Parser | Separate class | Single Responsibility; can be used independently of generator; supports forensic analysis of IDs |

## Error Handling

| Error Scenario | Handling Strategy |
|----------------|-------------------|
| **Clock moved backwards** | Raise `ClockMovedBackwardsError` with drift duration. Caller should wait or use monotonic clock. Generator refuses to produce IDs to prevent duplicates. |
| **Restart below persisted ceiling** | With `HighWaterMark`, wait (bounded by `overflow_timeout_seconds`) for the clock to pass the ceiling of the previous run; if the gap cannot close in time raise `ClockMovedBackwardsError`. A failed ceiling write raises and issues nothing. |
| **Sequence overflow** | Sleep until the next millisecond, rejecting rollback or deadline exhaustion. SequenceOverflowError is public; no fixed latency guarantee. |
| **Invalid configuration** | Raise `InvalidConfigError` at construction time. Fail-fast with descriptive message (which constraint was violated). |
| **Datacenter/machine ID too large** | Raise `InvalidConfigError` during config validation. Message includes max allowed value for configured bits. |
| **Bit allocation doesn't sum to 63** | Raise `InvalidConfigError` during config validation. Message shows actual sum vs expected 63. |
| **Negative or oversized ID for parsing** | Raise `ValueError` from `IDParser.parse()`. Validates input before attempting extraction. |
| **Batch count <= 0** | Raise `ValueError` from `generate_batch()`. Must be a positive integer. |
| **Timestamp exceeds max** | Reject offsets outside the configured time bits with ValueError. Changing epoch in an existing ID namespace requires a migration plan. |


## Current invariants and boundaries

The frozen configuration owns the layout/epoch/identity. Construction precomputes
sequence and timestamp limits, the timestamp shift and node prefix. Each generated
ID only reads the clock, validates and advances state, then composes these fields.
The same lock covers clock reads, bounded overflow waiting and state publication;
batch calls retain it for the entire batch. A failed batch can consume earlier IDs.

The parser requires an actual integer in 0..2**63-1 and the matching layout/epoch.
UTC conversion adds an integer millisecond timedelta to the Unix epoch, avoiding
floating seconds and OS timestamp limits. An unrepresentable Python datetime
raises ValueError, even when the numeric Snowflake ID itself fits the layout.
UUID and timestamp-random batches also require a positive integer count. The
latter requires a positive integer epoch and a 42-bit time offset; its output is
unsigned 64-bit and probabilistic, distinct from Snowflake's signed-range output.

Global uniqueness assumes a shared layout/epoch, distinct active identities and
no reuse of a time/sequence after restart. The optional [HighWaterMark](../src/unique_id/high_water.py)
covers restart: before issuing a timestamp above the durable ceiling the generator
persists `timestamp + window_ms` (temp file, fsync, atomic replace, directory fsync)
under its lock, so every returned ID is at or below a durable ceiling and the hot
path writes once per window. A new instance issues only timestamps above the loaded
ceiling, so a crash, same-millisecond restart or rollback cannot reissue an ID.
No identity lease, cross-process lock, NTP management or automatic failover is
implemented; one ceiling file must belong to one identity and one live generator.
MonotonicClock only protects its local lifetime against rollback; it cannot
synchronize machines or prevent identity reuse. Different layouts/epochs do not
form a shared globally unique namespace. See [measurements](../benchmarks/REPORT.md).
