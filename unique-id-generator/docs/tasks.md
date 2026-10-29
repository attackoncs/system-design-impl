# Tasks: Unique ID Generator

## Task Dependency Graph

```
Task 1 (Project Scaffolding)
  │
  ├──►Task 2 (Configuration)
  │      │
  ├──►Task 3 (Clock Abstraction)
  │      │
  │      ▼
  │    Task 4 (Snowflake Generator) ◄── Task 2, 3
  │      │
  ├──►Task 5 (ID Parser) ◄── Task 2
  │      │
  ├──►Task 6 (Alternative Strategies)
  │      │
  │      ▼
  │    Task 7 (Unit Tests) ◄── Task 2, 3, 4, 5, 6
  │      │
  │      ▼
  │    Task 8 (Property-Based Tests) ◄── Task 4, 5, 7
  │      │
  │      ▼
  └────►Task 9 (Examples & Documentation) ◄── Task 4, 5, 7
```

**Dependencies:**
- Task 1: No dependencies (foundation)
- Task 2: Depends on Task 1
- Task 3: Depends on Task 1
- Task 4: Depends on Tasks 2, 3
- Task 5: Depends on Task 2
- Task 6: Depends on Task 1
- Task 7: Depends on Tasks 2, 3, 4, 5, 6
- Task 8: Depends on Tasks 4, 5, 7
- Task 9: Depends on Tasks 4, 5, 7

---

## Task 1: Project Scaffolding

- [x] 1.1 Create `pyproject.toml` with hatchling build system
  - File: `unique-id-generator/pyproject.toml`
  - Python >= 3.9
  - No runtime dependencies (stdlib only)
  - Dev dependencies: `pytest`, `hypothesis`
  - Build target: `packages = ["src/unique_id"]`
  - Pytest config: `testpaths = ["tests"]`, `pythonpath = ["src"]`

- [x] 1.2 Create directory structure
  - Directories:
    - `unique-id-generator/src/unique_id/`
    - `unique-id-generator/tests/`
    - `unique-id-generator/examples/`
    - `unique-id-generator/docs/`
  - Create `__init__.py` files for `src/unique_id/` and `tests/`

- [x] 1.3 Create `src/unique_id/__init__.py` with public API exports
  - File: `unique-id-generator/src/unique_id/__init__.py`
  - Export: `SnowflakeGenerator`, `SnowflakeConfig`, `IDParser`, `ParsedID`
  - Export: `SystemClock`, `MonotonicClock`
  - Export: `UUIDGenerator`, `TimestampRandomGenerator`
  - Export: `ClockMovedBackwardsError`, `SequenceOverflowError`, `InvalidConfigError`
  - Version: `__version__ = "0.1.0"`

- [x] 1.4 Create `src/unique_id/exceptions.py` with custom exceptions
  - File: `unique-id-generator/src/unique_id/exceptions.py`
  - Classes: `UniqueIDError` (base), `ClockMovedBackwardsError`, `SequenceOverflowError`, `InvalidConfigError`
  - `ClockMovedBackwardsError`: stores `last_timestamp`, `current_timestamp`, `drift_ms`
  - `SequenceOverflowError`: stores `timestamp`, `max_sequence`
  - `InvalidConfigError`: stores descriptive message

- [x] 1.5 Verify project installs correctly
  - Run `pip install -e ".[dev]"` successfully
  - Run `pytest --collect-only` to verify test discovery

---

## Task 2: Configuration

- [x] 2.1 Implement `SnowflakeConfig` dataclass
  - File: `unique-id-generator/src/unique_id/config.py`
  - Frozen dataclass with fields: `timestamp_bits`, `datacenter_bits`, `machine_bits`, `sequence_bits`, `datacenter_id`, `machine_id`, `epoch_ms`
  - Default values: 41, 5, 5, 12, 0, 0, 1704067200000 (2024-01-01T00:00:00Z)
  - Computed properties: `max_datacenter_id`, `max_machine_id`, `max_sequence`, `max_timestamp`
  - Shift properties: `timestamp_shift`, `datacenter_shift`, `machine_shift`

- [x] 2.2 Implement configuration validation in `__post_init__`
  - Validate total bits == 63 (timestamp + datacenter + machine + sequence)
  - Validate all bit fields are positive integers
  - Validate `datacenter_id` <= `max_datacenter_id`
  - Validate `machine_id` <= `max_machine_id`
  - Validate `epoch_ms` is positive
  - Raise `InvalidConfigError` with descriptive messages on failure

- [x] 2.3 Write configuration tests
  - File: `unique-id-generator/tests/test_config.py`
  - Test: default config creates valid instance
  - Test: custom bit layout (e.g., 42+4+4+13) is accepted
  - Test: bits not summing to 63 raises `InvalidConfigError`
  - Test: datacenter_id exceeding max raises `InvalidConfigError`
  - Test: machine_id exceeding max raises `InvalidConfigError`
  - Test: negative bit values raise `InvalidConfigError`
  - Test: computed properties return correct values
  - Test: shift properties are correct for default and custom configs

---

## Task 3: Clock Abstraction

- [x] 3.1 Implement `Clock` abstract base class
  - File: `unique-id-generator/src/unique_id/clock.py`
  - Abstract method: `current_millis() -> int`
  - Import from `abc` module

- [x] 3.2 Implement `SystemClock`
  - File: `unique-id-generator/src/unique_id/clock.py`
  - Uses `time.time() * 1000` converted to int
  - Simple, no state

- [x] 3.3 Implement `MonotonicClock`
  - File: `unique-id-generator/src/unique_id/clock.py`
  - Anchors `time.monotonic()` to `time.time()` at initialization
  - `current_millis()` returns: `anchor_system_ms + (current_monotonic - anchor_monotonic)`
  - Guarantees monotonically increasing values

- [x] 3.4 Write clock tests
  - File: `unique-id-generator/tests/test_clock.py`
  - Test: `SystemClock.current_millis()` returns reasonable value (within last few seconds of `time.time() * 1000`)
  - Test: `MonotonicClock.current_millis()` returns reasonable value
  - Test: `MonotonicClock` values are monotonically increasing over multiple calls
  - Test: `SystemClock` values are approximately equal to system time
  - Test: both clocks implement the `Clock` interface

---

## Task 4: Snowflake Generator

- [x] 4.1 Implement `SnowflakeGenerator.__init__`
  - File: `unique-id-generator/src/unique_id/snowflake.py`
  - Accept optional `config` (default `SnowflakeConfig()`) and `clock` (default `SystemClock()`)
  - Initialize: `_sequence = 0`, `_last_timestamp = -1`, `_lock = threading.Lock()`

- [x] 4.2 Implement `SnowflakeGenerator.generate()`
  - Acquire lock
  - Get current timestamp from clock
  - Clock drift check: if `timestamp < last_timestamp`, raise `ClockMovedBackwardsError`
  - Same millisecond: increment sequence, check overflow → `_wait_next_millis()`
  - New millisecond: reset sequence to 0
  - Compose ID: `(ts_offset << timestamp_shift) | (dc << dc_shift) | (machine << machine_shift) | sequence`
  - Update `_last_timestamp`, release lock, return ID

- [x] 4.3 Implement `SnowflakeGenerator.generate_batch(count)`
  - Validate count > 0
  - Acquire lock once for entire batch
  - Loop `count` times using same logic as `generate()`
  - Return list of IDs

- [x] 4.4 Implement `SnowflakeGenerator._wait_next_millis(last_timestamp)`
  - Spin-wait: call `_current_millis()` until result > `last_timestamp`
  - Return the new timestamp

- [x] 4.5 Write Snowflake generator tests
  - File: `unique-id-generator/tests/test_snowflake.py`
  - Test: `generate()` returns a positive 64-bit integer
  - Test: consecutive IDs are strictly increasing
  - Test: IDs from different generators (different dc/machine) are different
  - Test: sequence increments within same millisecond (use mock clock)
  - Test: sequence overflow triggers wait-for-next-ms (use mock clock)
  - Test: clock moving backwards raises `ClockMovedBackwardsError` (use mock clock)
  - Test: `generate_batch(n)` returns `n` unique, increasing IDs
  - Test: `generate_batch(0)` raises `ValueError`
  - Test: thread safety — concurrent generation produces no duplicates
  - Use a `FakeClock` class for deterministic testing

---

## Task 5: ID Parser

- [x] 5.1 Implement `ParsedID` dataclass
  - File: `unique-id-generator/src/unique_id/parser.py`
  - Frozen dataclass with fields: `id_value`, `timestamp_ms`, `datacenter_id`, `machine_id`, `sequence`, `datetime_utc`
  - Custom `__repr__` for readable output

- [x] 5.2 Implement `IDParser.parse(id_value)`
  - File: `unique-id-generator/src/unique_id/parser.py`
  - Validate: id_value >= 0 and fits in 63 bits
  - Extract sequence: `id_value & sequence_mask`
  - Extract machine_id: `(id_value >> machine_shift) & machine_mask`
  - Extract datacenter_id: `(id_value >> datacenter_shift) & datacenter_mask`
  - Extract timestamp offset: `(id_value >> timestamp_shift) & timestamp_mask`
  - Convert timestamp: `offset + epoch_ms`
  - Convert to datetime: `datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)`
  - Return `ParsedID` instance

- [x] 5.3 Write parser tests
  - File: `unique-id-generator/tests/test_parser.py`
  - Test: parse a known ID and verify all components
  - Test: generate an ID then parse it — components match generator config
  - Test: parse extracts correct timestamp (convert back to datetime)
  - Test: parse extracts correct datacenter and machine IDs
  - Test: parse extracts correct sequence number
  - Test: negative ID raises `ValueError`
  - Test: ID exceeding 63 bits raises `ValueError`
  - Test: parser with custom config extracts correctly

---

## Task 6: Alternative Strategies

- [x] 6.1 Implement `IDGenerator` abstract base class
  - File: `unique-id-generator/src/unique_id/strategies.py`
  - Abstract methods: `generate()`, `generate_batch(count)`

- [x] 6.2 Implement `UUIDGenerator`
  - File: `unique-id-generator/src/unique_id/strategies.py`
  - `generate()`: returns `str(uuid.uuid4())`
  - `generate_batch(count)`: returns list of UUID strings

- [x] 6.3 Implement `TimestampRandomGenerator`
  - File: `unique-id-generator/src/unique_id/strategies.py`
  - Constructor: accept `epoch_ms` (default 2024-01-01)
  - `generate()`: `(timestamp_offset << 22) | random_22_bits`
  - `generate_batch(count)`: returns list of timestamp-random IDs
  - Use `random.SystemRandom()` for cryptographic randomness

- [x] 6.4 Write alternative strategy tests
  - File: `unique-id-generator/tests/test_strategies.py`
  - Test: `UUIDGenerator.generate()` returns valid UUID v4 string
  - Test: `UUIDGenerator.generate_batch(10)` returns 10 unique UUIDs
  - Test: `TimestampRandomGenerator.generate()` returns positive 64-bit int
  - Test: `TimestampRandomGenerator.generate_batch(10)` returns 10 IDs
  - Test: timestamp-random IDs have increasing timestamp component over time
  - Test: both generators implement `IDGenerator` interface

---

## Task 7: Unit Tests

- [x] 7.1 Implement `FakeClock` test helper
  - File: `unique-id-generator/tests/test_snowflake.py` (or `tests/conftest.py`)
  - Class `FakeClock(Clock)` with controllable `current_millis()` return value
  - Methods: `set_millis(ms)`, `advance(ms)` for deterministic testing

- [x] 7.2 Run full test suite and verify all pass
  - Command: `pytest tests/ -v`
  - All tests from Tasks 2.3, 3.4, 4.5, 5.3, 6.4 should pass
  - Verify no import errors or missing dependencies

- [x] 7.3 Add edge case tests
  - File: `unique-id-generator/tests/test_snowflake.py`
  - Test: maximum sequence number (4095 with default config) then overflow
  - Test: timestamp at epoch boundary (offset = 0)
  - Test: maximum datacenter and machine IDs (31, 31 with default config)
  - Test: custom config with different bit layout generates valid IDs
  - Test: two generators with same config but different machine IDs produce non-overlapping IDs

---

## Task 8: Property-Based Tests

- [x] 8.1 Write uniqueness property tests
  - File: `unique-id-generator/tests/test_properties.py`
  - **Property**: generating N IDs from the same generator produces N unique values
  - **Property**: generating IDs from two generators with different (dc, machine) pairs produces no duplicates
  - Use `hypothesis.given` with strategies for count (1-1000)

- [x] 8.2 Write monotonicity property tests
  - File: `unique-id-generator/tests/test_properties.py`
  - **Property**: IDs from the same generator are strictly monotonically increasing
  - **Property**: for any two IDs where id_a was generated before id_b, id_a < id_b
  - Use `hypothesis.given` with strategies for batch sizes

- [x] 8.3 Write time-ordering property tests
  - File: `unique-id-generator/tests/test_properties.py`
  - **Property**: parsed timestamp of a later-generated ID >= parsed timestamp of an earlier-generated ID
  - **Property**: IDs generated at time T1 < T2 have parsed timestamps where T1_parsed <= T2_parsed
  - Use `FakeClock` with hypothesis-generated timestamps

- [x] 8.4 Write bit layout correctness property tests
  - File: `unique-id-generator/tests/test_properties.py`
  - **Property**: for any generated ID, parsing it recovers the original datacenter_id and machine_id
  - **Property**: for any generated ID, the value fits in 63 bits (positive, < 2^63)
  - **Property**: for any valid config (bits sum to 63), generating and parsing roundtrips correctly
  - Use `hypothesis.given` with strategies for datacenter_id (0-31), machine_id (0-31)

- [x] 8.5 Write parser roundtrip property tests
  - File: `unique-id-generator/tests/test_properties.py`
  - **Property**: generate(config) → parse(id, config) → datacenter_id matches config.datacenter_id
  - **Property**: generate(config) → parse(id, config) → machine_id matches config.machine_id
  - **Property**: generate(config) → parse(id, config) → sequence is within [0, max_sequence]
  - **Property**: generate(config) → parse(id, config) → timestamp_ms >= config.epoch_ms

---

## Task 9: Examples and Documentation

- [x] 9.1 Create basic usage example
  - File: `unique-id-generator/examples/basic_usage.py`
  - Demonstrate: creating a generator with default config
  - Demonstrate: generating single IDs
  - Demonstrate: generating a batch of IDs
  - Demonstrate: parsing an ID back into components
  - Demonstrate: using MonotonicClock
  - Include comments explaining each step
  - Runnable with `python examples/basic_usage.py`

- [x] 9.2 Create multi-generator example
  - File: `unique-id-generator/examples/multi_generator.py`
  - Demonstrate: creating generators for different datacenters/machines
  - Demonstrate: generating IDs from multiple generators
  - Demonstrate: verifying uniqueness across generators
  - Demonstrate: parsing IDs to identify origin datacenter/machine
  - Demonstrate: custom bit layout configuration
  - Include comments explaining distributed deployment scenario

- [x] 9.3 Create `README.md`
  - File: `unique-id-generator/README.md`
  - Project overview and Snowflake algorithm explanation
  - Bit layout diagram (visual representation of 64-bit structure)
  - Installation instructions (`pip install -e ".[dev]"`)
  - Quick start guide (generate, parse, configure)
  - Configuration reference (all `SnowflakeConfig` fields)
  - API reference (SnowflakeGenerator, IDParser, alternative strategies)
  - Design decisions and trade-offs
  - Comparison table: Snowflake vs UUID v4 vs timestamp-random
  - Thread safety notes
  - Clock handling and NTP considerations

- [ ] 9.4 Copy documentation to docs/ directory
  - File: `unique-id-generator/docs/requirements.md` — copy from `.kiro/specs/unique-id-generator/requirements.md`
  - File: `unique-id-generator/docs/design.md` — copy from `.kiro/specs/unique-id-generator/design.md`
  - File: `unique-id-generator/docs/tasks.md` — copy from `.kiro/specs/unique-id-generator/tasks.md`
