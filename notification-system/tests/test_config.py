"""Unit tests for notification_system.config module."""

import pytest

from notification_system.config import (
    NotificationConfig,
    RateLimitConfig,
    RetryConfig,
)
from notification_system.models import Channel


class TestRateLimitConfig:
    """Tests for RateLimitConfig dataclass."""

    def test_default_max_count(self):
        config = RateLimitConfig()
        assert config.max_count == 100

    def test_default_window_seconds(self):
        config = RateLimitConfig()
        assert config.window_seconds == 3600.0

    def test_custom_values(self):
        config = RateLimitConfig(max_count=50, window_seconds=60.0)
        assert config.max_count == 50
        assert config.window_seconds == 60.0


class TestRetryConfig:
    """Tests for RetryConfig dataclass."""

    def test_default_max_retries(self):
        config = RetryConfig()
        assert config.max_retries == 3

    def test_default_base_delay(self):
        config = RetryConfig()
        assert config.base_delay == 1.0

    def test_default_max_delay(self):
        config = RetryConfig()
        assert config.max_delay == 300.0

    def test_default_jitter_factor(self):
        config = RetryConfig()
        assert config.jitter_factor == 0.1

    def test_custom_values(self):
        config = RetryConfig(max_retries=5, base_delay=2.0, max_delay=600.0, jitter_factor=0.2)
        assert config.max_retries == 5
        assert config.base_delay == 2.0
        assert config.max_delay == 600.0
        assert config.jitter_factor == 0.2


class TestNotificationConfigDefaults:
    """Tests for NotificationConfig default values."""

    def test_default_queue_max_depth(self):
        config = NotificationConfig()
        assert config.queue_max_depth == 10000

    def test_default_workers_per_channel(self):
        config = NotificationConfig()
        assert config.workers_per_channel == 3

    def test_default_dedup_window_seconds(self):
        config = NotificationConfig()
        assert config.dedup_window_seconds == 300.0

    def test_default_log_retention_seconds(self):
        config = NotificationConfig()
        assert config.log_retention_seconds == 86400.0 * 30

    def test_default_retry_config(self):
        config = NotificationConfig()
        assert config.retry.max_retries == 3
        assert config.retry.base_delay == 1.0
        assert config.retry.max_delay == 300.0

    def test_default_global_rate_limit(self):
        config = NotificationConfig()
        assert config.global_rate_limit.max_count == 100
        assert config.global_rate_limit.window_seconds == 3600.0

    def test_default_per_channel_rate_limits_has_all_channels(self):
        config = NotificationConfig()
        assert Channel.IOS_PUSH in config.per_channel_rate_limits
        assert Channel.ANDROID_PUSH in config.per_channel_rate_limits
        assert Channel.SMS in config.per_channel_rate_limits
        assert Channel.EMAIL in config.per_channel_rate_limits

    def test_default_ios_push_rate_limit(self):
        config = NotificationConfig()
        ios = config.per_channel_rate_limits[Channel.IOS_PUSH]
        assert ios.max_count == 50
        assert ios.window_seconds == 3600.0

    def test_default_sms_rate_limit(self):
        config = NotificationConfig()
        sms = config.per_channel_rate_limits[Channel.SMS]
        assert sms.max_count == 10
        assert sms.window_seconds == 3600.0

    def test_default_email_rate_limit(self):
        config = NotificationConfig()
        email = config.per_channel_rate_limits[Channel.EMAIL]
        assert email.max_count == 20
        assert email.window_seconds == 3600.0


class TestNotificationConfigValidation:
    """Tests for NotificationConfig __post_init__ validation."""

    def test_invalid_queue_max_depth_zero(self):
        with pytest.raises(ValueError, match="queue_max_depth must be positive"):
            NotificationConfig(queue_max_depth=0)

    def test_invalid_queue_max_depth_negative(self):
        with pytest.raises(ValueError, match="queue_max_depth must be positive"):
            NotificationConfig(queue_max_depth=-1)

    def test_invalid_workers_per_channel_zero(self):
        with pytest.raises(ValueError, match="workers_per_channel must be positive"):
            NotificationConfig(workers_per_channel=0)

    def test_invalid_workers_per_channel_negative(self):
        with pytest.raises(ValueError, match="workers_per_channel must be positive"):
            NotificationConfig(workers_per_channel=-5)

    def test_invalid_dedup_window_seconds_zero(self):
        with pytest.raises(ValueError, match="dedup_window_seconds must be positive"):
            NotificationConfig(dedup_window_seconds=0)

    def test_invalid_dedup_window_seconds_negative(self):
        with pytest.raises(ValueError, match="dedup_window_seconds must be positive"):
            NotificationConfig(dedup_window_seconds=-10.0)

    def test_invalid_max_retries_negative(self):
        with pytest.raises(ValueError, match="max_retries must be non-negative"):
            NotificationConfig(retry=RetryConfig(max_retries=-1))

    def test_valid_max_retries_zero(self):
        # Zero retries is valid (means no retries)
        config = NotificationConfig(retry=RetryConfig(max_retries=0))
        assert config.retry.max_retries == 0

    def test_invalid_base_delay_zero(self):
        with pytest.raises(ValueError, match="base_delay must be positive"):
            NotificationConfig(retry=RetryConfig(base_delay=0))

    def test_invalid_base_delay_negative(self):
        with pytest.raises(ValueError, match="base_delay must be positive"):
            NotificationConfig(retry=RetryConfig(base_delay=-1.0))

    def test_invalid_max_delay_zero(self):
        with pytest.raises(ValueError, match="max_delay must be positive"):
            NotificationConfig(retry=RetryConfig(max_delay=0))

    def test_invalid_max_delay_negative(self):
        with pytest.raises(ValueError, match="max_delay must be positive"):
            NotificationConfig(retry=RetryConfig(max_delay=-100.0))

    def test_valid_config_with_custom_values(self):
        config = NotificationConfig(
            queue_max_depth=5000,
            workers_per_channel=5,
            dedup_window_seconds=600.0,
            retry=RetryConfig(max_retries=5, base_delay=2.0, max_delay=600.0),
        )
        assert config.queue_max_depth == 5000
        assert config.workers_per_channel == 5
        assert config.dedup_window_seconds == 600.0
        assert config.retry.max_retries == 5
