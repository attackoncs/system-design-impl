"""Tests for SnowflakeGenerator."""

import threading

import pytest

from helpers import FakeClock
from unique_id.config import SnowflakeConfig
from unique_id.exceptions import ClockMovedBackwardsError
from unique_id.snowflake import SnowflakeGenerator


class TestSnowflakeGenerate:
    """Test basic ID generation."""

    def test_returns_positive_integer(self):
        clock = FakeClock(start_ms=1704067200000 + 1000)
        gen = SnowflakeGenerator(clock=clock)
        id_val = gen.generate()
        assert isinstance(id_val, int)
        assert id_val > 0

    def test_fits_in_64_bits(self):
        clock = FakeClock(start_ms=1704067200000 + 1000)
        gen = SnowflakeGenerator(clock=clock)
        id_val = gen.generate()
        assert id_val.bit_length() <= 63

    def test_consecutive_ids_strictly_increasing(self):
        clock = FakeClock(start_ms=1704067200000 + 1000)
        gen = SnowflakeGenerator(clock=clock)
        ids = [gen.generate() for _ in range(10)]
        for i in range(1, len(ids)):
            assert ids[i] > ids[i - 1]

    def test_different_generators_produce_different_ids(self):
        clock = FakeClock(start_ms=1704067200000 + 1000)
        config1 = SnowflakeConfig(datacenter_id=1, machine_id=1)
        config2 = SnowflakeConfig(datacenter_id=2, machine_id=2)
        gen1 = SnowflakeGenerator(config=config1, clock=clock)
        gen2 = SnowflakeGenerator(config=config2, clock=clock)
        id1 = gen1.generate()
        id2 = gen2.generate()
        assert id1 != id2

    def test_sequence_increments_same_millisecond(self):
        clock = FakeClock(start_ms=1704067200000 + 1000)
        gen = SnowflakeGenerator(clock=clock)
        id1 = gen.generate()
        id2 = gen.generate()
        # Same timestamp, sequence should differ by 1
        seq1 = id1 & 0xFFF  # lower 12 bits
        seq2 = id2 & 0xFFF
        assert seq1 == 0
        assert seq2 == 1

    def test_sequence_overflow_triggers_wait(self):
        clock = FakeClock(start_ms=1704067200000 + 1000)
        # 51 + 5 + 5 + 2 = 63, max_sequence = 3
        config = SnowflakeConfig(sequence_bits=2, timestamp_bits=51, datacenter_bits=5, machine_bits=5)
        gen = SnowflakeGenerator(config=config, clock=clock)

        # Generate 4 IDs (seq 0, 1, 2, 3) — fills sequence
        ids = []
        for _ in range(4):
            ids.append(gen.generate())

        # Next generate should trigger wait — advance clock
        clock.advance(1)
        id5 = gen.generate()
        assert id5 > ids[-1]

    def test_clock_backwards_raises_error(self):
        clock = FakeClock(start_ms=1704067200000 + 2000)
        gen = SnowflakeGenerator(clock=clock)
        gen.generate()

        # Move clock backwards
        clock.set_millis(1704067200000 + 1000)
        with pytest.raises(ClockMovedBackwardsError) as exc_info:
            gen.generate()
        assert exc_info.value.drift_ms == 1000


class TestSnowflakeGenerateBatch:
    """Test batch generation."""

    def test_returns_correct_count(self):
        clock = FakeClock(start_ms=1704067200000 + 1000)
        gen = SnowflakeGenerator(clock=clock)
        ids = gen.generate_batch(10)
        assert len(ids) == 10

    def test_batch_ids_unique(self):
        clock = FakeClock(start_ms=1704067200000 + 1000)
        gen = SnowflakeGenerator(clock=clock)
        ids = gen.generate_batch(100)
        assert len(set(ids)) == 100

    def test_batch_ids_increasing(self):
        clock = FakeClock(start_ms=1704067200000 + 1000)
        gen = SnowflakeGenerator(clock=clock)
        ids = gen.generate_batch(50)
        for i in range(1, len(ids)):
            assert ids[i] > ids[i - 1]

    def test_batch_zero_raises_value_error(self):
        gen = SnowflakeGenerator()
        with pytest.raises(ValueError, match="count must be positive"):
            gen.generate_batch(0)

    def test_batch_negative_raises_value_error(self):
        gen = SnowflakeGenerator()
        with pytest.raises(ValueError, match="count must be positive"):
            gen.generate_batch(-5)


class TestSnowflakeThreadSafety:
    """Test thread safety."""

    def test_concurrent_generation_no_duplicates(self):
        gen = SnowflakeGenerator()
        results = []
        lock = threading.Lock()

        def generate_ids(count):
            local_ids = []
            for _ in range(count):
                local_ids.append(gen.generate())
            with lock:
                results.extend(local_ids)

        threads = [threading.Thread(target=generate_ids, args=(100,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 400
        assert len(set(results)) == 400  # All unique


class TestSnowflakeEdgeCases:
    """Test edge cases."""

    def test_maximum_sequence_then_overflow(self):
        clock = FakeClock(start_ms=1704067200000 + 1000)
        gen = SnowflakeGenerator(clock=clock)
        config = gen.config

        # Generate max_sequence + 1 IDs to trigger overflow
        ids = []
        for i in range(config.max_sequence + 1):
            ids.append(gen.generate())

        # All should be unique
        assert len(set(ids)) == config.max_sequence + 1

        # Next one triggers wait — advance clock
        clock.advance(1)
        next_id = gen.generate()
        assert next_id > ids[-1]

    def test_timestamp_at_epoch_boundary(self):
        # Timestamp offset = 0 (exactly at epoch)
        config = SnowflakeConfig()
        clock = FakeClock(start_ms=config.epoch_ms)
        gen = SnowflakeGenerator(config=config, clock=clock)
        id_val = gen.generate()
        # Timestamp offset is 0, so ID should just be sequence (0)
        assert id_val == 0

    def test_maximum_datacenter_and_machine_ids(self):
        config = SnowflakeConfig(datacenter_id=31, machine_id=31)
        clock = FakeClock(start_ms=1704067200000 + 1000)
        gen = SnowflakeGenerator(config=config, clock=clock)
        id_val = gen.generate()
        assert id_val > 0
        # Verify datacenter and machine bits are set
        dc_bits = (id_val >> config.datacenter_shift) & config.max_datacenter_id
        machine_bits = (id_val >> config.machine_shift) & config.max_machine_id
        assert dc_bits == 31
        assert machine_bits == 31

    def test_custom_bit_layout_generates_valid_ids(self):
        config = SnowflakeConfig(
            timestamp_bits=42,
            datacenter_bits=4,
            machine_bits=4,
            sequence_bits=13,
            datacenter_id=10,
            machine_id=10,
        )
        clock = FakeClock(start_ms=1704067200000 + 5000)
        gen = SnowflakeGenerator(config=config, clock=clock)
        ids = [gen.generate() for _ in range(10)]
        assert len(set(ids)) == 10
        for id_val in ids:
            assert id_val > 0
            assert id_val.bit_length() <= 63

    def test_two_generators_same_config_different_machines_no_overlap(self):
        config1 = SnowflakeConfig(datacenter_id=1, machine_id=1)
        config2 = SnowflakeConfig(datacenter_id=1, machine_id=2)
        clock = FakeClock(start_ms=1704067200000 + 1000)
        gen1 = SnowflakeGenerator(config=config1, clock=clock)
        gen2 = SnowflakeGenerator(config=config2, clock=clock)

        ids1 = set(gen1.generate() for _ in range(100))
        ids2 = set(gen2.generate() for _ in range(100))
        assert ids1.isdisjoint(ids2)
