"""Property-based tests for the Snowflake ID generator.

Uses Hypothesis to verify universal properties hold across all inputs.
"""

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from helpers import FakeClock
from unique_id.config import SnowflakeConfig
from unique_id.parser import IDParser
from unique_id.snowflake import SnowflakeGenerator


# --- Strategies ---

valid_datacenter_id = st.integers(min_value=0, max_value=31)
valid_machine_id = st.integers(min_value=0, max_value=31)
batch_size = st.integers(min_value=1, max_value=200)
timestamp_offset = st.integers(min_value=1, max_value=1_000_000_000)


# --- 8.1 Uniqueness Property Tests ---


class TestUniquenessProperties:
    """Property tests for ID uniqueness.

    **Validates: Requirements NFR-2.1, NFR-2.2**
    """

    @given(count=st.integers(min_value=1, max_value=500))
    @settings(max_examples=50)
    def test_n_ids_from_same_generator_are_unique(self, count: int):
        """Property: generating N IDs from the same generator produces N unique values."""
        clock = FakeClock(start_ms=1704067200000 + 1000)
        gen = SnowflakeGenerator(clock=clock)
        ids = []
        for _ in range(count):
            ids.append(gen.generate())
            clock.advance(0)  # May or may not advance — sequence handles it
        assert len(set(ids)) == count

    @given(
        dc1=valid_datacenter_id,
        m1=valid_machine_id,
        dc2=valid_datacenter_id,
        m2=valid_machine_id,
        count=st.integers(min_value=1, max_value=100),
    )
    @settings(max_examples=50)
    def test_different_generators_no_duplicates(
        self, dc1: int, m1: int, dc2: int, m2: int, count: int
    ):
        """Property: IDs from two generators with different (dc, machine) produce no duplicates."""
        assume((dc1, m1) != (dc2, m2))

        clock = FakeClock(start_ms=1704067200000 + 1000)
        config1 = SnowflakeConfig(datacenter_id=dc1, machine_id=m1)
        config2 = SnowflakeConfig(datacenter_id=dc2, machine_id=m2)
        gen1 = SnowflakeGenerator(config=config1, clock=clock)
        gen2 = SnowflakeGenerator(config=config2, clock=clock)

        ids1 = set()
        ids2 = set()
        for _ in range(count):
            ids1.add(gen1.generate())
            ids2.add(gen2.generate())

        assert ids1.isdisjoint(ids2)


# --- 8.2 Monotonicity Property Tests ---


class TestMonotonicityProperties:
    """Property tests for ID monotonicity.

    **Validates: Requirements NFR-3.1, NFR-3.2, NFR-3.3**
    """

    @given(count=st.integers(min_value=2, max_value=500))
    @settings(max_examples=50)
    def test_ids_strictly_monotonically_increasing(self, count: int):
        """Property: IDs from the same generator are strictly monotonically increasing."""
        clock = FakeClock(start_ms=1704067200000 + 1000)
        gen = SnowflakeGenerator(clock=clock)
        ids = []
        for _ in range(count):
            ids.append(gen.generate())
        for i in range(1, len(ids)):
            assert ids[i] > ids[i - 1]

    @given(batch_size=st.integers(min_value=2, max_value=200))
    @settings(max_examples=50)
    def test_batch_ids_monotonically_increasing(self, batch_size: int):
        """Property: for any two IDs where id_a was generated before id_b, id_a < id_b."""
        clock = FakeClock(start_ms=1704067200000 + 1000)
        gen = SnowflakeGenerator(clock=clock)
        ids = gen.generate_batch(batch_size)
        for i in range(1, len(ids)):
            assert ids[i] > ids[i - 1]


# --- 8.3 Time-Ordering Property Tests ---


class TestTimeOrderingProperties:
    """Property tests for time ordering.

    **Validates: Requirements FR-1.4, NFR-3.2**
    """

    @given(
        ts_offset1=st.integers(min_value=1, max_value=500_000_000),
        ts_offset2=st.integers(min_value=1, max_value=500_000_000),
    )
    @settings(max_examples=50)
    def test_later_id_has_greater_or_equal_timestamp(
        self, ts_offset1: int, ts_offset2: int
    ):
        """Property: parsed timestamp of a later-generated ID >= earlier-generated ID."""
        assume(ts_offset1 <= ts_offset2)
        config = SnowflakeConfig()
        parser = IDParser(config=config)

        clock = FakeClock(start_ms=config.epoch_ms + ts_offset1)
        gen = SnowflakeGenerator(config=config, clock=clock)

        id1 = gen.generate()
        clock.set_millis(config.epoch_ms + ts_offset2)
        id2 = gen.generate()

        parsed1 = parser.parse(id1)
        parsed2 = parser.parse(id2)
        assert parsed2.timestamp_ms >= parsed1.timestamp_ms

    @given(
        ts1=st.integers(min_value=1, max_value=500_000_000),
        gap=st.integers(min_value=1, max_value=10000),
    )
    @settings(max_examples=50)
    def test_ids_at_different_times_have_ordered_timestamps(
        self, ts1: int, gap: int
    ):
        """Property: IDs generated at T1 < T2 have parsed timestamps T1_parsed <= T2_parsed."""
        config = SnowflakeConfig()
        parser = IDParser(config=config)
        ts2 = ts1 + gap

        clock = FakeClock(start_ms=config.epoch_ms + ts1)
        gen = SnowflakeGenerator(config=config, clock=clock)

        id1 = gen.generate()
        clock.set_millis(config.epoch_ms + ts2)
        id2 = gen.generate()

        parsed1 = parser.parse(id1)
        parsed2 = parser.parse(id2)
        assert parsed2.timestamp_ms >= parsed1.timestamp_ms


# --- 8.4 Bit Layout Correctness Property Tests ---


class TestBitLayoutProperties:
    """Property tests for bit layout correctness.

    **Validates: Requirements FR-1.1, FR-1.2, FR-2.2**
    """

    @given(dc=valid_datacenter_id, machine=valid_machine_id)
    @settings(max_examples=50)
    def test_parsing_recovers_datacenter_and_machine(self, dc: int, machine: int):
        """Property: parsing a generated ID recovers the original datacenter_id and machine_id."""
        config = SnowflakeConfig(datacenter_id=dc, machine_id=machine)
        clock = FakeClock(start_ms=1704067200000 + 5000)
        gen = SnowflakeGenerator(config=config, clock=clock)
        parser = IDParser(config=config)

        id_val = gen.generate()
        parsed = parser.parse(id_val)

        assert parsed.datacenter_id == dc
        assert parsed.machine_id == machine

    @given(dc=valid_datacenter_id, machine=valid_machine_id)
    @settings(max_examples=50)
    def test_generated_id_fits_in_63_bits(self, dc: int, machine: int):
        """Property: for any generated ID, the value fits in 63 bits (positive, < 2^63)."""
        config = SnowflakeConfig(datacenter_id=dc, machine_id=machine)
        clock = FakeClock(start_ms=1704067200000 + 5000)
        gen = SnowflakeGenerator(config=config, clock=clock)

        id_val = gen.generate()
        assert id_val >= 0
        assert id_val < (1 << 63)

    @given(
        dc=st.integers(min_value=0, max_value=15),
        machine=st.integers(min_value=0, max_value=15),
    )
    @settings(max_examples=50)
    def test_custom_config_roundtrip(self, dc: int, machine: int):
        """Property: for valid config (bits sum to 63), generating and parsing roundtrips."""
        config = SnowflakeConfig(
            timestamp_bits=42,
            datacenter_bits=4,
            machine_bits=4,
            sequence_bits=13,
            datacenter_id=dc,
            machine_id=machine,
        )
        clock = FakeClock(start_ms=1704067200000 + 5000)
        gen = SnowflakeGenerator(config=config, clock=clock)
        parser = IDParser(config=config)

        id_val = gen.generate()
        parsed = parser.parse(id_val)

        assert parsed.datacenter_id == dc
        assert parsed.machine_id == machine
        assert parsed.timestamp_ms == 1704067200000 + 5000
        assert parsed.sequence == 0


# --- 8.5 Parser Roundtrip Property Tests ---


class TestParserRoundtripProperties:
    """Property tests for parser roundtrip correctness.

    **Validates: Requirements FR-6.1, FR-6.2, FR-6.3, FR-6.4**
    """

    @given(dc=valid_datacenter_id, machine=valid_machine_id)
    @settings(max_examples=50)
    def test_roundtrip_datacenter_matches(self, dc: int, machine: int):
        """Property: generate → parse → datacenter_id matches config.datacenter_id."""
        config = SnowflakeConfig(datacenter_id=dc, machine_id=machine)
        clock = FakeClock(start_ms=1704067200000 + 3000)
        gen = SnowflakeGenerator(config=config, clock=clock)
        parser = IDParser(config=config)

        id_val = gen.generate()
        parsed = parser.parse(id_val)
        assert parsed.datacenter_id == config.datacenter_id

    @given(dc=valid_datacenter_id, machine=valid_machine_id)
    @settings(max_examples=50)
    def test_roundtrip_machine_matches(self, dc: int, machine: int):
        """Property: generate → parse → machine_id matches config.machine_id."""
        config = SnowflakeConfig(datacenter_id=dc, machine_id=machine)
        clock = FakeClock(start_ms=1704067200000 + 3000)
        gen = SnowflakeGenerator(config=config, clock=clock)
        parser = IDParser(config=config)

        id_val = gen.generate()
        parsed = parser.parse(id_val)
        assert parsed.machine_id == config.machine_id

    @given(
        dc=valid_datacenter_id,
        machine=valid_machine_id,
        count=st.integers(min_value=1, max_value=50),
    )
    @settings(max_examples=50)
    def test_roundtrip_sequence_in_range(self, dc: int, machine: int, count: int):
        """Property: generate → parse → sequence is within [0, max_sequence]."""
        config = SnowflakeConfig(datacenter_id=dc, machine_id=machine)
        clock = FakeClock(start_ms=1704067200000 + 3000)
        gen = SnowflakeGenerator(config=config, clock=clock)
        parser = IDParser(config=config)

        for _ in range(count):
            id_val = gen.generate()
            parsed = parser.parse(id_val)
            assert 0 <= parsed.sequence <= config.max_sequence

    @given(dc=valid_datacenter_id, machine=valid_machine_id)
    @settings(max_examples=50)
    def test_roundtrip_timestamp_gte_epoch(self, dc: int, machine: int):
        """Property: generate → parse → timestamp_ms >= config.epoch_ms."""
        config = SnowflakeConfig(datacenter_id=dc, machine_id=machine)
        clock = FakeClock(start_ms=1704067200000 + 3000)
        gen = SnowflakeGenerator(config=config, clock=clock)
        parser = IDParser(config=config)

        id_val = gen.generate()
        parsed = parser.parse(id_val)
        assert parsed.timestamp_ms >= config.epoch_ms
