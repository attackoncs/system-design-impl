"""Configuration dataclasses for the notification system.

Defines RateLimitConfig, RetryConfig, and NotificationConfig with sensible
defaults and __post_init__ validation.
"""

from __future__ import annotations

import dataclasses
import math

import notification_system.models


def _positive(name, value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive and finite")


def _integer(name, value, minimum=1):
    if type(value) is not int or value < minimum:
        rule = "non-negative" if minimum == 0 else "positive"
        raise ValueError(f"{name} must be {rule} integer")


@dataclasses.dataclass
class RateLimitConfig:
    """Rate limit configuration for a single scope."""

    max_count: int = 100
    window_seconds: float = 3600.0

    def __post_init__(self):
        _integer("max_count", self.max_count)
        _positive("window_seconds", self.window_seconds)


@dataclasses.dataclass
class RetryConfig:
    """Retry configuration for failed deliveries."""

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 300.0
    jitter_factor: float = 0.1

    def __post_init__(self):
        _integer("max_retries", self.max_retries, 0)
        _positive("base_delay", self.base_delay)
        _positive("max_delay", self.max_delay)
        if (
            type(self.jitter_factor) not in (int, float)
            or not math.isfinite(self.jitter_factor)
            or not 0 <= self.jitter_factor <= 1
        ):
            raise ValueError("jitter_factor must be finite and between 0 and 1")


@dataclasses.dataclass
class NotificationConfig:
    """Configuration for the notification system.

    All numeric values must be positive. Raises ValueError on invalid input.
    """

    # Queue settings
    queue_max_depth: int = 10000

    # Worker settings
    workers_per_channel: int = 3

    # Rate limiting
    per_channel_rate_limits: dict[
        notification_system.models.Channel, RateLimitConfig
    ] = dataclasses.field(
        default_factory=lambda: {
            notification_system.models.Channel.IOS_PUSH: RateLimitConfig(
                max_count=50, window_seconds=3600.0
            ),
            notification_system.models.Channel.ANDROID_PUSH: RateLimitConfig(
                max_count=50, window_seconds=3600.0
            ),
            notification_system.models.Channel.SMS: RateLimitConfig(
                max_count=10, window_seconds=3600.0
            ),
            notification_system.models.Channel.EMAIL: RateLimitConfig(
                max_count=20, window_seconds=3600.0
            ),
        }
    )
    global_rate_limit: RateLimitConfig = dataclasses.field(
        default_factory=lambda: RateLimitConfig(max_count=100, window_seconds=3600.0)
    )

    # Deduplication
    dedup_window_seconds: float = 300.0

    # Retry
    retry: RetryConfig = dataclasses.field(default_factory=RetryConfig)

    # Log retention
    log_retention_seconds: float = 86400.0 * 30  # 30 days

    def __post_init__(self) -> None:
        """Validate configuration values."""
        _integer("queue_max_depth", self.queue_max_depth)
        _integer("workers_per_channel", self.workers_per_channel)
        _positive("dedup_window_seconds", self.dedup_window_seconds)
        _positive("log_retention_seconds", self.log_retention_seconds)
        if not isinstance(self.retry, RetryConfig):
            raise ValueError("retry must be RetryConfig")
        self.retry.__post_init__()
        if not isinstance(self.global_rate_limit, RateLimitConfig):
            raise ValueError("global_rate_limit must be RateLimitConfig")
        self.global_rate_limit.__post_init__()
        if not isinstance(self.per_channel_rate_limits, dict):
            raise ValueError("per_channel_rate_limits must be a dictionary")
        for channel, limit in self.per_channel_rate_limits.items():
            if not isinstance(
                channel, notification_system.models.Channel
            ) or not isinstance(limit, RateLimitConfig):
                raise ValueError(
                    "per_channel_rate_limits must map Channel to RateLimitConfig"
                )
            limit.__post_init__()
