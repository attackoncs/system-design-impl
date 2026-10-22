"""Tests for SnowflakeConfig."""

import pytest
import unique_id.config
import unique_id.exceptions


class TestSnowflakeConfigDefaults:
    """Test default configuration."""

    def test_default_config_creates_valid_instance(self):
        config = unique_id.config.SnowflakeConfig()
        assert config.timestamp_bits == 41
        assert config.datacenter_bits == 5
        assert config.machine_bits == 5
        assert config.sequence_bits == 12
        assert config.datacenter_id == 0
        assert config.machine_id == 0
        assert config.epoch_ms == unique_id.config.DEFAULT_EPOCH_MS

    def test_default_config_bits_sum_to_63(self):
        config = unique_id.config.SnowflakeConfig()
        total = (
            config.timestamp_bits
            + config.datacenter_bits
            + config.machine_bits
            + config.sequence_bits
        )
        assert total == 63


class TestSnowflakeConfigCustom:
    """Test custom configurations."""

    def test_custom_bit_layout_accepted(self):
        config = unique_id.config.SnowflakeConfig(
            timestamp_bits=42,
            datacenter_bits=4,
            machine_bits=4,
            sequence_bits=13,
        )
        assert config.timestamp_bits == 42
        assert config.datacenter_bits == 4
        assert config.machine_bits == 4
        assert config.sequence_bits == 13

    def test_custom_ids(self):
        config = unique_id.config.SnowflakeConfig(datacenter_id=10, machine_id=20)
        assert config.datacenter_id == 10
        assert config.machine_id == 20


class TestSnowflakeConfigValidation:
    """Test configuration validation."""

    def test_bits_not_summing_to_63_raises(self):
        with pytest.raises(unique_id.exceptions.InvalidConfigError, match="sum to 63"):
            unique_id.config.SnowflakeConfig(
                timestamp_bits=40,
                datacenter_bits=5,
                machine_bits=5,
                sequence_bits=12,
            )

    def test_datacenter_id_exceeding_max_raises(self):
        with pytest.raises(unique_id.exceptions.InvalidConfigError, match="datacenter_id"):
            unique_id.config.SnowflakeConfig(datacenter_id=32)  # max is 31 for 5 bits

    def test_machine_id_exceeding_max_raises(self):
        with pytest.raises(unique_id.exceptions.InvalidConfigError, match="machine_id"):
            unique_id.config.SnowflakeConfig(machine_id=32)  # max is 31 for 5 bits

    def test_negative_timestamp_bits_raises(self):
        with pytest.raises(
            unique_id.exceptions.InvalidConfigError, match="timestamp_bits must be positive"
        ):
            unique_id.config.SnowflakeConfig(
                timestamp_bits=-1,
                datacenter_bits=5,
                machine_bits=5,
                sequence_bits=54,
            )

    def test_negative_datacenter_bits_raises(self):
        with pytest.raises(
            unique_id.exceptions.InvalidConfigError, match="datacenter_bits must be positive"
        ):
            unique_id.config.SnowflakeConfig(
                timestamp_bits=54,
                datacenter_bits=-1,
                machine_bits=5,
                sequence_bits=5,
            )

    def test_negative_machine_bits_raises(self):
        with pytest.raises(
            unique_id.exceptions.InvalidConfigError, match="machine_bits must be positive"
        ):
            unique_id.config.SnowflakeConfig(
                timestamp_bits=54,
                datacenter_bits=5,
                machine_bits=-1,
                sequence_bits=5,
            )

    def test_negative_sequence_bits_raises(self):
        with pytest.raises(
            unique_id.exceptions.InvalidConfigError, match="sequence_bits must be positive"
        ):
            unique_id.config.SnowflakeConfig(
                timestamp_bits=54,
                datacenter_bits=5,
                machine_bits=5,
                sequence_bits=-1,
            )

    def test_zero_bits_raises(self):
        with pytest.raises(unique_id.exceptions.InvalidConfigError):
            unique_id.config.SnowflakeConfig(
                timestamp_bits=0,
                datacenter_bits=5,
                machine_bits=5,
                sequence_bits=53,
            )

    def test_negative_epoch_raises(self):
        with pytest.raises(
            unique_id.exceptions.InvalidConfigError, match="epoch_ms must be positive"
        ):
            unique_id.config.SnowflakeConfig(epoch_ms=-1)


class TestSnowflakeConfigProperties:
    """Test computed properties."""

    def test_max_datacenter_id(self):
        config = unique_id.config.SnowflakeConfig()
        assert config.max_datacenter_id == 31  # 2^5 - 1

    def test_max_machine_id(self):
        config = unique_id.config.SnowflakeConfig()
        assert config.max_machine_id == 31  # 2^5 - 1

    def test_max_sequence(self):
        config = unique_id.config.SnowflakeConfig()
        assert config.max_sequence == 4095  # 2^12 - 1

    def test_max_timestamp(self):
        config = unique_id.config.SnowflakeConfig()
        assert config.max_timestamp == (1 << 41) - 1

    def test_timestamp_shift_default(self):
        config = unique_id.config.SnowflakeConfig()
        assert config.timestamp_shift == 22  # 5 + 5 + 12

    def test_datacenter_shift_default(self):
        config = unique_id.config.SnowflakeConfig()
        assert config.datacenter_shift == 17  # 5 + 12

    def test_machine_shift_default(self):
        config = unique_id.config.SnowflakeConfig()
        assert config.machine_shift == 12

    def test_shifts_custom_config(self):
        config = unique_id.config.SnowflakeConfig(
            timestamp_bits=42,
            datacenter_bits=4,
            machine_bits=4,
            sequence_bits=13,
        )
        assert config.timestamp_shift == 21  # 4 + 4 + 13
        assert config.datacenter_shift == 17  # 4 + 13
        assert config.machine_shift == 13
