"""Basic usage example for the Unique ID Generator.

Demonstrates:
- Creating a generator with default config
- Generating single IDs
- Generating a batch of IDs
- Parsing an ID back into components
- Using MonotonicClock

Run: python examples/basic_usage.py
"""

import sys
from pathlib import Path

# Allow running directly without pip install
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unique_id import (
    SnowflakeGenerator,
    SnowflakeConfig,
    IDParser,
    MonotonicClock,
)


def main():
    # --- 1. Create a generator with default configuration ---
    print("=== Basic Snowflake ID Generation ===\n")

    generator = SnowflakeGenerator()
    print(f"Generator config: {generator.config}")
    print()

    # --- 2. Generate single IDs ---
    print("--- Single ID Generation ---")
    for i in range(5):
        id_val = generator.generate()
        print(f"  ID {i + 1}: {id_val}")
    print()

    # --- 3. Generate a batch of IDs ---
    print("--- Batch Generation (10 IDs) ---")
    batch = generator.generate_batch(10)
    for i, id_val in enumerate(batch):
        print(f"  Batch ID {i + 1}: {id_val}")
    print()

    # --- 4. Parse an ID back into components ---
    print("--- ID Parsing ---")
    parser = IDParser()
    sample_id = generator.generate()
    parsed = parser.parse(sample_id)
    print(f"  Generated ID: {sample_id}")
    print(f"  Parsed: {parsed}")
    print(f"    Timestamp (ms): {parsed.timestamp_ms}")
    print(f"    Datacenter ID:  {parsed.datacenter_id}")
    print(f"    Machine ID:     {parsed.machine_id}")
    print(f"    Sequence:       {parsed.sequence}")
    print(f"    DateTime (UTC): {parsed.datetime_utc}")
    print()

    # --- 5. Using MonotonicClock ---
    print("--- Using MonotonicClock ---")
    mono_gen = SnowflakeGenerator(clock=MonotonicClock())
    mono_ids = [mono_gen.generate() for _ in range(5)]
    print("  IDs with MonotonicClock (guaranteed no backwards movement):")
    for i, id_val in enumerate(mono_ids):
        print(f"    ID {i + 1}: {id_val}")
    print()

    # --- 6. Custom configuration ---
    print("--- Custom Configuration ---")
    custom_config = SnowflakeConfig(
        datacenter_id=5,
        machine_id=10,
    )
    custom_gen = SnowflakeGenerator(config=custom_config)
    custom_id = custom_gen.generate()
    custom_parsed = parser.parse(custom_id)
    print(f"  Config: datacenter=5, machine=10")
    print(f"  Generated ID: {custom_id}")
    # Use parser with matching config
    custom_parser = IDParser(config=custom_config)
    custom_parsed = custom_parser.parse(custom_id)
    print(f"  Parsed datacenter: {custom_parsed.datacenter_id}")
    print(f"  Parsed machine:    {custom_parsed.machine_id}")


if __name__ == "__main__":
    main()
