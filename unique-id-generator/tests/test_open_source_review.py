"""Precision and public input boundaries found during the chapter 8 review."""

import datetime

import pytest

from helpers import FakeClock
from unique_id import IDParser, SnowflakeConfig, SnowflakeGenerator
from unique_id.strategies import TimestampRandomGenerator, UUIDGenerator


@pytest.mark.parametrize("value", [True, False, 1.5, "1", None])
def test_parser_requires_an_integer_id(value):
    with pytest.raises(ValueError, match="integer"):
        IDParser().parse(value)


@pytest.mark.parametrize(
    "timestamp", [32503680000123, 253402300799998, 253402300799999]
)
def test_custom_layout_preserves_exact_datetime_milliseconds(timestamp):
    config = SnowflakeConfig(timestamp_bits=48, sequence_bits=5)
    generator = SnowflakeGenerator(config, FakeClock(timestamp))
    parsed = IDParser(config).parse(generator.generate())
    expected = datetime.datetime(
        1970, 1, 1, tzinfo=datetime.timezone.utc
    ) + datetime.timedelta(milliseconds=timestamp)
    assert parsed.timestamp_ms == timestamp
    assert parsed.datetime_utc == expected


def test_datetime_overflow_has_a_consistent_public_error():
    config = SnowflakeConfig(timestamp_bits=48, sequence_bits=5)
    generator = SnowflakeGenerator(config, FakeClock(253402300800000))
    with pytest.raises(ValueError, match="datetime range"):
        IDParser(config).parse(generator.generate())


@pytest.mark.parametrize("epoch", [True, 1.5, "1", 0, -1])
def test_timestamp_random_requires_a_positive_integer_epoch(epoch):
    with pytest.raises(ValueError, match="epoch_ms"):
        TimestampRandomGenerator(epoch)


@pytest.mark.parametrize("offset", [-1, 1 << 42])
def test_timestamp_random_rejects_out_of_range_time(monkeypatch, offset):
    epoch = SnowflakeConfig().epoch_ms
    monkeypatch.setattr(
        "unique_id.strategies.time.time", lambda: (epoch + offset) / 1000
    )
    with pytest.raises(ValueError, match="timestamp"):
        TimestampRandomGenerator(epoch).generate()


@pytest.mark.parametrize("offset", [0, (1 << 42) - 1])
def test_timestamp_random_accepts_both_time_boundaries(monkeypatch, offset):
    epoch = SnowflakeConfig().epoch_ms
    monkeypatch.setattr(
        "unique_id.strategies.time.time", lambda: (epoch + offset) / 1000
    )
    value = TimestampRandomGenerator(epoch).generate()
    assert 0 <= value < 1 << 64
    assert value >> 22 == offset


@pytest.mark.parametrize("strategy", [UUIDGenerator, TimestampRandomGenerator])
@pytest.mark.parametrize("count", [True, 0, -1, 1.5, "1"])
def test_alternative_batch_counts_match_snowflake_contract(strategy, count):
    with pytest.raises(ValueError, match="count"):
        strategy().generate_batch(count)
