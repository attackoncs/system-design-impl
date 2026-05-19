# Unique ID Generator

A Python implementation of the Twitter Snowflake algorithm for generating 64-bit unique, time-sortable IDs suitable for distributed systems. Designed for high-throughput scenarios requiring guaranteed uniqueness, strict monotonicity, and embedded machine identity.

## Features

- **64-bit unique IDs** — compact integers usable as database primary keys
- **Time-sortable** — IDs generated later always have higher numeric values
- **Strictly monotonic** — no duplicates, no out-of-order IDs within a generator
- **Configurable bit layout** — tune timestamp range, datacenter/machine capacity, and throughput
- **Thread-safe** — concurrent generation via `threading.Lock`
- **Clock abstraction** — SystemClock or MonotonicClock for drift-prone environments
- **ID parsing** — decompose any ID back into timestamp, datacenter, machine, and sequence
- **Alternative strategies** — UUID v4 and timestamp-random generators for comparison
- **Zero runtime dependencies** — stdlib only

## The Snowflake Algorithm

The Snowflake algorithm (originally developed at Twitter) generates unique IDs by combining a timestamp, machine identity, and a per-millisecond sequence counter into a single 64-bit integer.

Key insight: by embedding time in the high-order bits, IDs are naturally sorted by creation time without requiring a centralized coordinator.

### Bit Layout (Default: 41 + 5 + 5 + 12)

```
 63  62                              22  21       17  16       12  11            0
┌───┬───────────────────────────────────┬───────────┬───────────┬───────────────┐
│ 0 │         Timestamp (41 bits)       │  DC (5)   │ Mach (5)  │ Sequence (12) │
└───┴───────────────────────────────────┴───────────┴───────────┴───────────────┘
  │                  │                        │           │             │
  │                  │                        │           │             └─ 4096 IDs/ms/machine
  │                  │                        │           └─ 32 machines per datacenter
  │                  │                        └─ 32 datacenters
  │                  └─ Milliseconds since epoch (~69 years)
  └─ Sign bit (always 0, ensures positive integer)
```

**Capacity with default layout:**

| Component | Bits | Range | Capacity |
|-----------|------|-------|----------|
| Sign | 1 | Always 0 | Positive integers only |
| Timestamp | 41 | 0 to 2^41 - 1 | ~69.7 years from epoch |
| Datacenter | 5 | 0 to 31 | 32 datacenters |
| Machine | 5 | 0 to 31 | 32 machines per DC |
| Sequence | 12 | 0 to 4095 | 4,096 IDs per ms per machine |

**Total throughput**: 4,096 × 1,000 = **4.096 million IDs/second** per machine.

### ID Composition

```
id = (timestamp_offset << 22) | (datacenter_id << 17) | (machine_id << 12) | sequence
```

Where `timestamp_offset = current_time_ms - epoch_ms`.

## Installation

```bash
cd unique-id-generator
pip install -e .
```

With development dependencies (pytest, hypothesis):

```bash
pip install -e ".[dev]"
```

### Requirements

- Python >= 3.9
- No runtime dependencies (stdlib only)

## Quick Start

### Generate IDs

```python
from unique_id import SnowflakeGenerator

# Create a generator with default config
generator = SnowflakeGenerator()

# Generate a single ID
id_val = generator.generate()
print(id_val)  # e.g., 7089025953792000

# Generate a batch of IDs (more efficient — single lock acquisition)
ids = generator.generate_batch(100)
print(f"Generated {len(ids)} unique IDs")
print(f"All increasing: {ids == sorted(ids)}")  # True
```

### Parse IDs

```python
from unique_id import SnowflakeGenerator, IDParser

generator = SnowflakeGenerator()
id_val = generator.generate()

# Parse an ID back into its components
parser = IDParser()
parsed = parser.parse(id_val)

print(parsed.timestamp_ms)    # Absolute timestamp in milliseconds
print(parsed.datacenter_id)   # 0
print(parsed.machine_id)      # 0
print(parsed.sequence)        # 0
print(parsed.datetime_utc)    # datetime(2024, ..., tzinfo=UTC)
```

### Configure for Your Deployment

```python
from unique_id import SnowflakeConfig, SnowflakeGenerator

# Assign datacenter and machine identity
config = SnowflakeConfig(
    datacenter_id=3,
    machine_id=17,
)
gen = SnowflakeGenerator(config=config)

# Use MonotonicClock for environments with NTP drift
from unique_id import MonotonicClock

gen = SnowflakeGenerator(config=config, clock=MonotonicClock())
```

## Configuration Reference

### SnowflakeConfig Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `timestamp_bits` | `int` | 41 | Bits allocated for timestamp (~69 years with 41) |
| `datacenter_bits` | `int` | 5 | Bits for datacenter ID (max 32 DCs with 5) |
| `machine_bits` | `int` | 5 | Bits for machine ID (max 32 machines with 5) |
| `sequence_bits` | `int` | 12 | Bits for sequence counter (4096/ms with 12) |
| `datacenter_id` | `int` | 0 | This generator's datacenter identifier |
| `machine_id` | `int` | 0 | This generator's machine identifier |
| `epoch_ms` | `int` | 1704067200000 | Custom epoch in ms (default: 2024-01-01T00:00:00Z) |

**Constraint**: `timestamp_bits + datacenter_bits + machine_bits + sequence_bits == 63`

### Computed Properties

| Property | Description |
|----------|-------------|
| `max_datacenter_id` | Maximum datacenter ID (2^datacenter_bits - 1) |
| `max_machine_id` | Maximum machine ID (2^machine_bits - 1) |
| `max_sequence` | Maximum sequence number (2^sequence_bits - 1) |
| `max_timestamp` | Maximum timestamp offset (2^timestamp_bits - 1) |
| `timestamp_shift` | Bit shift for timestamp component |
| `datacenter_shift` | Bit shift for datacenter component |
| `machine_shift` | Bit shift for machine component |

### Custom Bit Layouts

```python
from unique_id import SnowflakeConfig

# More machines, fewer datacenters
config = SnowflakeConfig(
    timestamp_bits=42,    # ~139 years from epoch
    datacenter_bits=3,    # 8 datacenters
    machine_bits=6,       # 64 machines per DC
    sequence_bits=12,     # 4096 IDs/ms
    datacenter_id=3,
    machine_id=42,
)

# Higher throughput per machine
config = SnowflakeConfig(
    timestamp_bits=39,    # ~17 years from epoch
    datacenter_bits=5,    # 32 datacenters
    machine_bits=5,       # 32 machines
    sequence_bits=14,     # 16,384 IDs/ms per machine
)
```

### Validation

Configuration is validated at construction time. Invalid configs raise `InvalidConfigError`:

- Bit fields must be positive integers
- Bits must sum to exactly 63
- `datacenter_id` must fit in `datacenter_bits`
- `machine_id` must fit in `machine_bits`
- `epoch_ms` must be positive

## API Reference

### SnowflakeGenerator

The core ID generator. Thread-safe.

```python
class SnowflakeGenerator:
    def __init__(self, config: SnowflakeConfig = None, clock: Clock = None): ...
    def generate(self) -> int: ...
    def generate_batch(self, count: int) -> list[int]: ...
    @property
    def config(self) -> SnowflakeConfig: ...
```

| Method | Description |
|--------|-------------|
| `generate()` | Generate a single unique 64-bit ID |
| `generate_batch(count)` | Generate `count` IDs efficiently (single lock) |
| `config` | Access the generator's configuration |

**Raises:**
- `ClockMovedBackwardsError` — if the clock moves backwards (drift detected)
- `ValueError` — if `count <= 0` in `generate_batch`

### IDParser

Decomposes Snowflake IDs into their constituent parts.

```python
class IDParser:
    def __init__(self, config: SnowflakeConfig = None): ...
    def parse(self, id_value: int) -> ParsedID: ...
```

**ParsedID fields:**

| Field | Type | Description |
|-------|------|-------------|
| `id_value` | `int` | The original ID |
| `timestamp_ms` | `int` | Absolute timestamp (epoch + offset) |
| `datacenter_id` | `int` | Extracted datacenter ID |
| `machine_id` | `int` | Extracted machine ID |
| `sequence` | `int` | Extracted sequence number |
| `datetime_utc` | `datetime` | Timestamp as UTC datetime |

**Raises:**
- `ValueError` — if `id_value` is negative or exceeds 63 bits

### Alternative Strategies

All strategies implement the `IDGenerator` abstract base class with `generate()` and `generate_batch(count)` methods.

```python
from unique_id import UUIDGenerator, TimestampRandomGenerator

# UUID v4: 128-bit random, globally unique, no coordination
uuid_gen = UUIDGenerator()
uuid_str = uuid_gen.generate()  # "550e8400-e29b-41d4-a716-446655440000"

# Timestamp-random: 64-bit, simple, probabilistic uniqueness
ts_gen = TimestampRandomGenerator(epoch_ms=1704067200000)
ts_id = ts_gen.generate()  # 64-bit integer
```

### Clock Implementations

```python
from unique_id import SystemClock, MonotonicClock

# SystemClock: uses time.time(), subject to NTP adjustments
clock = SystemClock()

# MonotonicClock: uses time.monotonic() anchored to system time
# Cannot go backwards — recommended for drift-prone environments
clock = MonotonicClock()

# Pass to generator
gen = SnowflakeGenerator(clock=clock)
```

## Comparison: Snowflake vs UUID v4 vs Timestamp-Random

| Feature | Snowflake | UUID v4 | Timestamp-Random |
|---------|-----------|---------|------------------|
| **Size** | 64-bit int | 128-bit string (36 chars) | 64-bit int |
| **Time-sortable** | ✓ | ✗ | Partially (same ms unordered) |
| **Strictly monotonic** | ✓ | ✗ | ✗ |
| **Uniqueness** | Guaranteed (per config) | Probabilistic (2^122 space) | Probabilistic (2^22 random) |
| **Coordination** | Per machine (dc+machine ID) | None needed | None needed |
| **Configuration** | Required (dc/machine ID) | None | Optional (epoch) |
| **DB index performance** | Excellent (sequential) | Poor (random) | Good (time-prefix) |
| **Machine identity** | Embedded in ID | None | None |
| **Clock dependency** | Yes (millisecond) | No | Yes (millisecond) |
| **Collision risk** | Zero (within config) | ~2^-61 per pair | ~2^-22 per ms |
| **Max throughput/machine** | 4.096M IDs/sec | Unlimited | Unlimited |

**When to use each:**
- **Snowflake**: High-throughput systems needing sortable IDs with machine traceability
- **UUID v4**: Systems where simplicity and zero-coordination matter more than sortability
- **Timestamp-Random**: Simple systems needing roughly-sorted 64-bit IDs without machine assignment

## Thread Safety

`SnowflakeGenerator` is fully thread-safe for concurrent use from multiple threads.

**Implementation details:**
- A `threading.Lock` protects the mutable state (`_sequence` and `_last_timestamp`)
- The lock is held only during state mutation (minimal critical section)
- `generate_batch(count)` acquires the lock once for the entire batch, reducing contention compared to calling `generate()` in a loop

**Multi-threaded usage:**

```python
import threading
from unique_id import SnowflakeGenerator

gen = SnowflakeGenerator()
results = []
lock = threading.Lock()

def worker():
    ids = gen.generate_batch(1000)
    with lock:
        results.extend(ids)

threads = [threading.Thread(target=worker) for _ in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()

# All IDs are unique
assert len(results) == len(set(results))
```

## Clock Handling and NTP Considerations

### The Clock Drift Problem

In distributed systems, NTP (Network Time Protocol) can adjust the system clock backwards to correct drift. If the Snowflake generator uses a timestamp that has already been used, it could produce duplicate IDs.

### How This Library Handles It

1. **Detection**: The generator tracks `_last_timestamp`. If the current clock reading is less than the last timestamp, clock drift is detected.

2. **Fail-fast**: A `ClockMovedBackwardsError` is raised immediately, refusing to generate IDs that could be duplicates. The error includes the drift duration for diagnostics.

3. **MonotonicClock alternative**: For environments where NTP adjustments are frequent, use `MonotonicClock`. It anchors `time.monotonic()` to system time at initialization and cannot go backwards.

### Recommendations

| Scenario | Recommendation |
|----------|---------------|
| Well-configured NTP (< 1ms drift) | `SystemClock` (default) is fine |
| Frequent NTP adjustments | Use `MonotonicClock` |
| VM migration / clock jumps | Use `MonotonicClock` |
| Long-running processes | `MonotonicClock` avoids accumulated drift |
| Short-lived processes | `SystemClock` is simpler |

### Sequence Overflow Handling

When more than 4,096 IDs (default config) are generated within a single millisecond, the sequence counter overflows. The generator handles this by spin-waiting until the next millisecond:

```
generate() called → sequence at max → spin-wait → next ms → reset sequence → generate
```

This ensures no ID loss at the cost of a brief latency spike (< 1ms). In practice, overflow only occurs at sustained rates above 4 million IDs/second per machine.

## Error Handling

| Error | When Raised | Recovery Strategy |
|-------|-------------|-------------------|
| `ClockMovedBackwardsError` | Clock drift detected | Wait for drift duration, or use `MonotonicClock` |
| `InvalidConfigError` | Bad configuration at construction | Fix config parameters |
| `SequenceOverflowError` | Internal (handled automatically) | Generator waits for next ms |
| `ValueError` | Invalid parse input or batch count | Check input values |

```python
from unique_id import SnowflakeGenerator, ClockMovedBackwardsError

gen = SnowflakeGenerator()

try:
    id_val = gen.generate()
except ClockMovedBackwardsError as e:
    print(f"Clock moved back by {e.drift_ms}ms")
    # Option 1: wait and retry
    import time
    time.sleep(e.drift_ms / 1000.0)
    id_val = gen.generate()
```

## Design Decisions and Trade-offs

| Decision | Choice | Rationale |
|----------|--------|-----------|
| ID type | Plain `int` | No string conversion overhead; directly usable as DB primary key |
| Thread safety | `threading.Lock` | Simple, correct; GIL provides some safety but lock ensures correctness |
| Clock drift | Raise exception (fail-fast) | Prevents duplicate IDs; caller decides recovery strategy |
| Sequence overflow | Spin-wait for next ms | Ensures no ID loss; < 1ms latency acceptable for rare event |
| Configuration | Frozen dataclass | Immutable after creation; validated at construction time |
| Bit layout | Configurable with sensible defaults | Default matches Twitter Snowflake; tunable for different scale needs |
| Custom epoch | 2024-01-01T00:00:00Z | Recent epoch maximizes usable timestamp range (~69 years) |
| Clock abstraction | ABC with injection | Enables deterministic testing; supports monotonic clock |
| Batch generation | Single lock acquisition | Reduces overhead for bulk generation; maintains ordering |
| Parser | Separate class | Single responsibility; usable independently for forensic analysis |
| No runtime deps | stdlib only | Minimal footprint; no supply chain risk |

## Running Tests

```bash
pip install -e ".[dev]"

# Run all tests
pytest tests/ -v

# Run specific test modules
pytest tests/test_snowflake.py -v    # Core generator tests
pytest tests/test_config.py -v       # Configuration tests
pytest tests/test_parser.py -v       # Parser tests
pytest tests/test_properties.py -v   # Property-based tests (Hypothesis)

# Run with Hypothesis statistics
pytest tests/test_properties.py --hypothesis-show-statistics
```

### Test Coverage

- **Unit tests**: Core generation, configuration validation, parsing, clock behavior, edge cases
- **Property-based tests** (Hypothesis): Uniqueness, monotonicity, time-ordering, bit layout correctness, parser roundtrip

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
│       ├── __init__.py         # Public API exports
│       ├── snowflake.py        # Core Snowflake generator
│       ├── config.py           # SnowflakeConfig dataclass
│       ├── clock.py            # Clock abstraction (System, Monotonic)
│       ├── parser.py           # ID parsing/decomposition
│       ├── strategies.py       # Alternative strategies (UUID, timestamp-random)
│       └── exceptions.py       # Custom exceptions
├── tests/
│   ├── __init__.py
│   ├── test_snowflake.py       # Generator unit tests
│   ├── test_config.py          # Configuration tests
│   ├── test_clock.py           # Clock tests
│   ├── test_parser.py          # Parser tests
│   ├── test_strategies.py      # Alternative strategy tests
│   └── test_properties.py      # Property-based tests (Hypothesis)
└── examples/
    ├── basic_usage.py          # Single generator usage
    └── multi_generator.py      # Multi-datacenter scenario
```

## References

- Twitter Snowflake (original concept, 2010)
- Alex Xu, "System Design Interview" — Chapter 7: Design a Unique ID Generator

## License

MIT
