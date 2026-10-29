# Requirements: Unique ID Generator

## Overview

A Python unique ID generator library implementing the Twitter Snowflake approach from System Design Interview Chapter 8. Generates 64-bit unique IDs that are time-sortable, numerically ordered, and suitable for distributed systems. Supports configurable bit layouts, multiple generators per datacenter/machine, clock drift handling, batch generation, and ID parsing. Provides alternative strategies (UUID v4, timestamp-random) for comparison and fallback scenarios.

## Functional Requirements

### FR-1: ID Generation (Snowflake Algorithm)

The system shall generate unique 64-bit IDs using the Twitter Snowflake approach:

- **FR-1.1**: Each ID shall be a 64-bit positive integer (sign bit always 0).
- **FR-1.2**: IDs shall be composed of: sign bit (1) + timestamp (41) + datacenter ID (5) + machine ID (5) + sequence number (12) by default.
- **FR-1.3**: The timestamp component shall represent milliseconds since a configurable custom epoch.
- **FR-1.4**: IDs generated later in time shall have higher numeric values (time-sortable).
- **FR-1.5**: The sequence number shall increment for IDs generated within the same millisecond on the same machine.
- **FR-1.6**: The sequence number shall reset to 0 at the start of each new millisecond.
- **FR-1.7**: IDs shall be numerical only (no hyphens, letters, or special characters).

### FR-2: Bit Layout Configuration

- **FR-2.1**: The bit allocation for timestamp, datacenter ID, machine ID, and sequence number shall be configurable.
- **FR-2.2**: The total bits (excluding sign bit) shall always sum to 63.
- **FR-2.3**: The custom epoch shall be configurable (default: a project-specific epoch such as 2024-01-01T00:00:00Z).
- **FR-2.4**: Configuration shall validate that all bit allocations are positive integers and sum correctly.
- **FR-2.5**: Configuration shall validate that datacenter ID and machine ID fit within their allocated bits.
- **FR-2.6**: The default configuration shall support: 41 bits timestamp (~69 years), 5 bits datacenter (32 datacenters), 5 bits machine (32 machines per datacenter), 12 bits sequence (4096 IDs per millisecond per machine).

### FR-3: Multi-Generator Support

- **FR-3.1**: The system shall support creating multiple generator instances with different datacenter and machine IDs.
- **FR-3.2**: Each generator instance shall be independently configurable with its own bit layout and epoch.
- **FR-3.3**: IDs from different generators (different datacenter/machine combinations) shall be globally unique without coordination.
- **FR-3.4**: The system shall reject generator creation with datacenter or machine IDs that exceed the configured bit capacity.

### FR-4: Clock Handling

- **FR-4.1**: The generator shall use monotonic time tracking to ensure IDs are always increasing.
- **FR-4.2**: If the system clock moves backwards (clock drift detected), the generator shall raise a `ClockMovedBackwardsError` with the drift duration.
- **FR-4.3**: When the sequence number overflows within a millisecond (exceeds max for configured bits), the generator shall wait until the next millisecond before generating the next ID.
- **FR-4.4**: The clock abstraction shall support both system clock and monotonic clock implementations.
- **FR-4.5**: The generator shall track the last timestamp used to detect backward clock movement.

### FR-5: Batch Generation

- **FR-5.1**: The system shall provide a `generate_batch(count)` method to efficiently generate multiple IDs in a single call.
- **FR-5.2**: Batch-generated IDs shall maintain all uniqueness and ordering guarantees.
- **FR-5.3**: Batch generation shall be more efficient than calling `generate()` in a loop (reduced lock acquisition overhead).

### FR-6: ID Parsing

- **FR-6.1**: The system shall provide an `IDParser` that decomposes a Snowflake ID into its constituent parts: timestamp, datacenter ID, machine ID, and sequence number.
- **FR-6.2**: The parser shall convert the extracted timestamp back to a Python `datetime` object.
- **FR-6.3**: The parser shall use the same configuration (bit layout, epoch) as the generator that created the ID.
- **FR-6.4**: The parser shall return a structured `ParsedID` object with all extracted fields.

### FR-7: Thread Safety

- **FR-7.1**: The generator shall be safe for concurrent use from multiple threads.
- **FR-7.2**: Thread safety shall be achieved using a threading lock to protect sequence number and timestamp state.
- **FR-7.3**: The lock shall be held for the minimum duration necessary (only during state mutation).

### FR-8: Alternative Strategies

- **FR-8.1**: The system shall provide a UUID v4 generator as an alternative strategy (128-bit random, no ordering guarantee).
- **FR-8.2**: The system shall provide a timestamp-random generator that combines a millisecond timestamp prefix with random bits for simplicity.
- **FR-8.3**: Alternative strategies shall implement a common interface for interchangeability.
- **FR-8.4**: Alternative strategies shall document their trade-offs compared to Snowflake (uniqueness guarantees, sortability, size).

## Non-Functional Requirements

### NFR-1: Performance

- **NFR-1.1**: The generator shall produce more than 10,000 IDs per second on a single thread.
- **NFR-1.2**: Single ID generation shall complete in under 1 millisecond (excluding wait-for-next-ms scenarios).
- **NFR-1.3**: Batch generation of 1,000 IDs shall complete in under 100 milliseconds.
- **NFR-1.4**: Lock contention under multi-threaded access shall not degrade throughput below 10,000 IDs/sec.

### NFR-2: Uniqueness Guarantee

- **NFR-2.1**: No duplicate IDs shall be generated within the same generator instance (same datacenter + machine).
- **NFR-2.2**: No duplicate IDs shall be generated across different generators when datacenter/machine ID combinations are unique.
- **NFR-2.3**: Uniqueness shall hold for the lifetime of the configured epoch (~69 years with 41-bit timestamp).

### NFR-3: Monotonicity

- **NFR-3.1**: IDs generated by the same generator instance shall be strictly monotonically increasing.
- **NFR-3.2**: IDs generated at a later wall-clock time shall have a higher timestamp component than IDs generated earlier (barring clock drift).
- **NFR-3.3**: Within the same millisecond, IDs shall be ordered by sequence number.

### NFR-4: Project Structure

- **NFR-4.1**: The project shall use `pyproject.toml` with hatchling build system.
- **NFR-4.2**: The project shall use src layout (`src/unique_id/`).
- **NFR-4.3**: The project shall require Python >= 3.9.
- **NFR-4.4**: The project shall include type hints throughout.
- **NFR-4.5**: The project shall include unit tests using pytest.
- **NFR-4.6**: The project shall include property-based tests using Hypothesis.
- **NFR-4.7**: The project shall provide usage examples demonstrating basic and multi-generator scenarios.
