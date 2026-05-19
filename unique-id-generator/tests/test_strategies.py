"""Tests for alternative ID generation strategies."""

import uuid

from unique_id.strategies import IDGenerator, UUIDGenerator, TimestampRandomGenerator


class TestUUIDGenerator:
    """Test UUID v4 generator."""

    def test_generate_returns_valid_uuid_string(self):
        gen = UUIDGenerator()
        result = gen.generate()
        # Should be a valid UUID v4 string
        parsed = uuid.UUID(result)
        assert parsed.version == 4

    def test_generate_batch_returns_correct_count(self):
        gen = UUIDGenerator()
        results = gen.generate_batch(10)
        assert len(results) == 10

    def test_generate_batch_all_unique(self):
        gen = UUIDGenerator()
        results = gen.generate_batch(10)
        assert len(set(results)) == 10

    def test_implements_id_generator_interface(self):
        gen = UUIDGenerator()
        assert isinstance(gen, IDGenerator)


class TestTimestampRandomGenerator:
    """Test timestamp-random generator."""

    def test_generate_returns_positive_integer(self):
        gen = TimestampRandomGenerator()
        result = gen.generate()
        assert isinstance(result, int)
        assert result > 0

    def test_generate_fits_in_64_bits(self):
        gen = TimestampRandomGenerator()
        result = gen.generate()
        assert result.bit_length() <= 64

    def test_generate_batch_returns_correct_count(self):
        gen = TimestampRandomGenerator()
        results = gen.generate_batch(10)
        assert len(results) == 10

    def test_timestamp_component_increases_over_time(self):
        import time

        gen = TimestampRandomGenerator()
        id1 = gen.generate()
        time.sleep(0.01)  # 10ms
        id2 = gen.generate()
        # Timestamp is upper bits, so later ID should generally be larger
        ts1 = id1 >> 22
        ts2 = id2 >> 22
        assert ts2 >= ts1

    def test_implements_id_generator_interface(self):
        gen = TimestampRandomGenerator()
        assert isinstance(gen, IDGenerator)
