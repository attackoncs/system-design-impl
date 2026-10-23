"""Boundary checks share the single and batch generation state machine."""
from types import SimpleNamespace
import pytest
from unique_id.config import SnowflakeConfig
from unique_id.snowflake import SnowflakeGenerator


@pytest.mark.parametrize('batch', [False, True])
@pytest.mark.parametrize('offset', [-1, 1 << 41])
def test_timestamp_must_fit_layout(batch, offset):
    config = SnowflakeConfig()
    clock = SimpleNamespace(current_millis=lambda: config.epoch_ms + offset)
    generator = SnowflakeGenerator(config, clock)
    with pytest.raises(ValueError, match='timestamp'):
        generator.generate_batch(1) if batch else generator.generate()
