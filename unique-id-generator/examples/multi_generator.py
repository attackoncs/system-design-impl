"""Multi-generator example for the Unique ID Generator.

Demonstrates a distributed deployment scenario where multiple Snowflake
generators run across different datacenters and machines. Each generator
produces globally unique IDs without any coordination between nodes.

Scenario:
  - 3 datacenters (US-East, EU-West, AP-South)
  - Each datacenter has multiple machines
  - All generators produce IDs independently
  - IDs are globally unique and can be traced back to their origin

Run: python examples/multi_generator.py
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
    # =========================================================================
    # 1. Creating generators for different datacenters and machines
    # =========================================================================
    print("=== Multi-Generator Distributed Deployment ===\n")
    print("--- Setting up generators across 3 datacenters ---")

    # In a real deployment, each service instance would create ONE generator
    # with its assigned datacenter_id and machine_id. Here we simulate
    # multiple nodes for demonstration.

    # Datacenter 0: US-East (machines 0, 1)
    us_east_m0 = SnowflakeGenerator(
        config=SnowflakeConfig(datacenter_id=0, machine_id=0)
    )
    us_east_m1 = SnowflakeGenerator(
        config=SnowflakeConfig(datacenter_id=0, machine_id=1)
    )

    # Datacenter 1: EU-West (machines 0, 1)
    eu_west_m0 = SnowflakeGenerator(
        config=SnowflakeConfig(datacenter_id=1, machine_id=0)
    )
    eu_west_m1 = SnowflakeGenerator(
        config=SnowflakeConfig(datacenter_id=1, machine_id=1)
    )

    # Datacenter 2: AP-South (machine 0)
    ap_south_m0 = SnowflakeGenerator(
        config=SnowflakeConfig(datacenter_id=2, machine_id=0)
    )

    generators = {
        "US-East/Machine-0": us_east_m0,
        "US-East/Machine-1": us_east_m1,
        "EU-West/Machine-0": eu_west_m0,
        "EU-West/Machine-1": eu_west_m1,
        "AP-South/Machine-0": ap_south_m0,
    }

    print(f"  Created {len(generators)} generators across 3 datacenters")
    print()

    # =========================================================================
    # 2. Generating IDs from multiple generators
    # =========================================================================
    print("--- Generating IDs from each generator ---")

    all_ids: list[tuple[str, int]] = []  # (origin, id)

    for name, gen in generators.items():
        ids = gen.generate_batch(5)
        print(f"  {name}:")
        for id_val in ids:
            print(f"    {id_val}")
            all_ids.append((name, id_val))
    print()

    # =========================================================================
    # 3. Verifying uniqueness across all generators
    # =========================================================================
    print("--- Verifying Global Uniqueness ---")

    id_values = [id_val for _, id_val in all_ids]
    total_ids = len(id_values)
    unique_ids = len(set(id_values))

    print(f"  Total IDs generated: {total_ids}")
    print(f"  Unique IDs:          {unique_ids}")
    print(f"  Duplicates:          {total_ids - unique_ids}")

    if total_ids == unique_ids:
        print("  ✓ All IDs are globally unique (no coordination needed!)")
    else:
        print("  ✗ Duplicates detected (this should never happen)")
    print()

    # =========================================================================
    # 4. Parsing IDs to identify origin datacenter/machine
    # =========================================================================
    print("--- Parsing IDs to Identify Origin ---")
    print("  (Useful for debugging, routing, and forensic analysis)")
    print()

    # Datacenter name lookup
    datacenter_names = {0: "US-East", 1: "EU-West", 2: "AP-South"}

    parser = IDParser()  # Default config matches our generators

    # Parse a sample from each generator
    sample_ids = [
        all_ids[0],   # First ID from US-East/Machine-0
        all_ids[5],   # First ID from US-East/Machine-1
        all_ids[10],  # First ID from EU-West/Machine-0
        all_ids[15],  # First ID from EU-West/Machine-1
        all_ids[20],  # First ID from AP-South/Machine-0
    ]

    for origin, id_val in sample_ids:
        parsed = parser.parse(id_val)
        dc_name = datacenter_names.get(parsed.datacenter_id, "Unknown")
        print(f"  ID: {id_val}")
        print(f"    Origin (actual):  {origin}")
        print(f"    Parsed datacenter: {parsed.datacenter_id} ({dc_name})")
        print(f"    Parsed machine:    {parsed.machine_id}")
        print(f"    Timestamp:         {parsed.datetime_utc.isoformat()}")
        print(f"    Sequence:          {parsed.sequence}")
        print()

    # =========================================================================
    # 5. Custom bit layout configuration
    # =========================================================================
    print("--- Custom Bit Layout Configuration ---")
    print()

    # Scenario: A deployment with fewer datacenters but more machines per DC
    # and higher throughput (more sequence bits).
    #
    # Default layout: 41 timestamp + 5 datacenter + 5 machine + 12 sequence = 63
    # Custom layout:  42 timestamp + 3 datacenter + 4 machine + 14 sequence = 63
    #
    # Trade-offs:
    #   - 42-bit timestamp: ~139 years from epoch (vs ~69 years)
    #   - 3-bit datacenter: 8 datacenters max (vs 32)
    #   - 4-bit machine: 16 machines per DC (vs 32)
    #   - 14-bit sequence: 16,384 IDs/ms/machine (vs 4,096)

    custom_config = SnowflakeConfig(
        timestamp_bits=42,
        datacenter_bits=3,
        machine_bits=4,
        sequence_bits=14,
        datacenter_id=2,
        machine_id=7,
    )

    print(f"  Custom layout: {custom_config.timestamp_bits}t + "
          f"{custom_config.datacenter_bits}dc + "
          f"{custom_config.machine_bits}m + "
          f"{custom_config.sequence_bits}seq = 63 bits")
    print(f"  Max datacenters:     {custom_config.max_datacenter_id + 1} "
          f"(vs 32 default)")
    print(f"  Max machines per DC: {custom_config.max_machine_id + 1} "
          f"(vs 32 default)")
    print(f"  Max IDs per ms:      {custom_config.max_sequence + 1} "
          f"(vs 4096 default)")
    print(f"  Epoch lifespan:      ~{2 ** custom_config.timestamp_bits // (365.25 * 24 * 3600 * 1000):.0f} years "
          f"(vs ~69 years default)")
    print()

    # Generate and parse with custom config
    custom_gen = SnowflakeGenerator(config=custom_config, clock=MonotonicClock())
    custom_parser = IDParser(config=custom_config)

    custom_ids = custom_gen.generate_batch(5)
    print("  Generated IDs with custom layout:")
    for id_val in custom_ids:
        parsed = custom_parser.parse(id_val)
        print(f"    ID: {id_val}  →  dc={parsed.datacenter_id}, "
              f"machine={parsed.machine_id}, seq={parsed.sequence}")
    print()

    # =========================================================================
    # Summary
    # =========================================================================
    print("=== Summary ===")
    print()
    print("  Key takeaways for distributed deployments:")
    print("  • Each node gets a unique (datacenter_id, machine_id) pair")
    print("  • No coordination between nodes is needed for uniqueness")
    print("  • IDs can be parsed to trace back to their origin node")
    print("  • Custom bit layouts let you tune for your scale requirements")
    print("  • MonotonicClock prevents issues from NTP clock adjustments")


if __name__ == "__main__":
    main()
