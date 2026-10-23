import concurrent.futures
import pytest
from unique_id.config import SnowflakeConfig
from unique_id.snowflake import SnowflakeGenerator
from unique_id.exceptions import (
    InvalidConfigError,
    SequenceOverflowError,
    ClockMovedBackwardsError,
)
from tests.helpers import FakeClock


@pytest.mark.parametrize(
    "field", ["timestamp_bits", "machine_id", "datacenter_id", "epoch_ms"]
)
@pytest.mark.parametrize("value", [True, 1.5, "1"])
def test_config_requires_integers(field, value):
    with pytest.raises(InvalidConfigError):
        SnowflakeConfig(**{field: value})


def test_overflow_timeout_does_not_reuse_ids():
    config = SnowflakeConfig(timestamp_bits=52, sequence_bits=1)
    clock = FakeClock()
    generator = SnowflakeGenerator(config, clock, overflow_timeout_seconds=0)
    first = generator.generate_batch(2)
    with pytest.raises(SequenceOverflowError):
        generator.generate()
    clock.advance(1)
    assert generator.generate() > first[-1]


def test_clock_reversal_during_overflow_is_rejected():
    class ReversingClock(FakeClock):
        calls = 0

        def current_millis(self):
            self.calls += 1
            return self._current_ms - (self.calls >= 4)

    generator = SnowflakeGenerator(
        SnowflakeConfig(timestamp_bits=52, sequence_bits=1), ReversingClock()
    )
    generator.generate_batch(2)
    with pytest.raises(ClockMovedBackwardsError):
        generator.generate()


def test_threads_share_sequence_without_duplicates():
    generator = SnowflakeGenerator(clock=FakeClock())
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        batches = list(pool.map(lambda _: generator.generate_batch(100), range(16)))
    ids = [value for batch in batches for value in batch]
    assert len(set(ids)) == 1600
    assert all(batch == sorted(batch) for batch in batches)
