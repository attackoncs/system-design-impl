"""Tests for IDParser."""

import helpers
import pytest
import unique_id.config
import unique_id.parser
import unique_id.snowflake


class TestIDParser:
    """Test ID parsing."""

    def test_parse_known_id(self):
        config = unique_id.config.SnowflakeConfig(datacenter_id=5, machine_id=10)
        clock = helpers.FakeClock(start_ms=1704067200000 + 5000)
        gen = unique_id.snowflake.SnowflakeGenerator(config=config, clock=clock)
        id_val = gen.generate()

        parser = unique_id.parser.IDParser(config=config)
        parsed = parser.parse(id_val)

        assert parsed.id_value == id_val
        assert parsed.datacenter_id == 5
        assert parsed.machine_id == 10
        assert parsed.sequence == 0
        assert parsed.timestamp_ms == 1704067200000 + 5000

    def test_generate_then_parse_matches_config(self):
        config = unique_id.config.SnowflakeConfig(datacenter_id=15, machine_id=20)
        clock = helpers.FakeClock(start_ms=1704067200000 + 10000)
        gen = unique_id.snowflake.SnowflakeGenerator(config=config, clock=clock)
        parser = unique_id.parser.IDParser(config=config)

        id_val = gen.generate()
        parsed = parser.parse(id_val)

        assert parsed.datacenter_id == config.datacenter_id
        assert parsed.machine_id == config.machine_id

    def test_parse_extracts_correct_timestamp(self):
        config = unique_id.config.SnowflakeConfig()
        timestamp_ms = 1704067200000 + 123456
        clock = helpers.FakeClock(start_ms=timestamp_ms)
        gen = unique_id.snowflake.SnowflakeGenerator(config=config, clock=clock)
        parser = unique_id.parser.IDParser(config=config)

        id_val = gen.generate()
        parsed = parser.parse(id_val)

        assert parsed.timestamp_ms == timestamp_ms
        # Verify datetime conversion
        assert parsed.datetime_utc.year >= 2024

    def test_parse_extracts_correct_sequence(self):
        config = unique_id.config.SnowflakeConfig()
        clock = helpers.FakeClock(start_ms=1704067200000 + 1000)
        gen = unique_id.snowflake.SnowflakeGenerator(config=config, clock=clock)
        parser = unique_id.parser.IDParser(config=config)

        # Generate multiple IDs in same ms
        ids = [gen.generate() for _ in range(5)]
        for i, id_val in enumerate(ids):
            parsed = parser.parse(id_val)
            assert parsed.sequence == i

    def test_negative_id_raises_value_error(self):
        parser = unique_id.parser.IDParser()
        with pytest.raises(ValueError, match="non-negative"):
            parser.parse(-1)

    def test_id_exceeding_63_bits_raises_value_error(self):
        parser = unique_id.parser.IDParser()
        with pytest.raises(ValueError, match="exceeds 63 bits"):
            parser.parse(2**63)

    def test_parser_with_custom_config(self):
        config = unique_id.config.SnowflakeConfig(
            timestamp_bits=42,
            datacenter_bits=4,
            machine_bits=4,
            sequence_bits=13,
            datacenter_id=10,
            machine_id=12,
        )
        clock = helpers.FakeClock(start_ms=1704067200000 + 7777)
        gen = unique_id.snowflake.SnowflakeGenerator(config=config, clock=clock)
        parser = unique_id.parser.IDParser(config=config)

        id_val = gen.generate()
        parsed = parser.parse(id_val)

        assert parsed.datacenter_id == 10
        assert parsed.machine_id == 12
        assert parsed.timestamp_ms == 1704067200000 + 7777
        assert parsed.sequence == 0

    def test_parsed_id_repr(self):
        config = unique_id.config.SnowflakeConfig()
        clock = helpers.FakeClock(start_ms=1704067200000 + 1000)
        gen = unique_id.snowflake.SnowflakeGenerator(config=config, clock=clock)
        parser = unique_id.parser.IDParser(config=config)

        id_val = gen.generate()
        parsed = parser.parse(id_val)
        repr_str = repr(parsed)
        assert "ParsedID" in repr_str
        assert "datacenter=" in repr_str
        assert "machine=" in repr_str
