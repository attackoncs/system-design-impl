"""Configuration dataclasses for the notification system.

Defines RateLimitConfig, RetryConfig, and NotificationConfig with sensible
defaults and __post_init__ validation.
"""

from __future__ import annotations

import dataclasses

import notification_system.models


@dataclasses.dataclass
class RateLimitConfig:
    """Rate limit configuration for a single scope."""

    max_count: int = 100
    window_seconds: float = 3600.0


@dataclasses.dataclass
class RetryConfig:
    """Retry configuration for failed deliveries."""

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 300.0
    jitter_factor: float = 0.1


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
        default_factory=lambda: RateLimitConfig(
            max_count=100, window_seconds=3600.0
        )
    )

    # Deduplication
    dedup_window_seconds: float = 300.0

    # Retry
    retry: RetryConfig = dataclasses.field(default_factory=RetryConfig)

    # Log retention
    log_retention_seconds: float = 86400.0 * 30  # 30 days

    def __post_init__(self) -> None:
        """Validate configuration values."""
        if self.queue_max_depth <= 0:
            raise ValueError(
                f"queue_max_depth must be positive, got {self.queue_max_depth}"
            )
        if self.workers_per_channel <= 0:
            raise ValueError(
                "workers_per_channel must be positive, got"
                f" {self.workers_per_channel}"
            )
        if self.dedup_window_seconds <= 0:
            raise ValueError(
                "dedup_window_seconds must be positive, got"
                f" {self.dedup_window_seconds}"
            )
        if self.retry.max_retries < 0:
            raise ValueError(
                "max_retries must be non-negative, got"
                f" {self.retry.max_retries}"
            )
        if self.retry.base_delay <= 0:
            raise ValueError(
                f"base_delay must be positive, got {self.retry.base_delay}"
            )
        if self.retry.max_delay <= 0:
            raise ValueError(
                f"max_delay must be positive, got {self.retry.max_delay}"
            )
