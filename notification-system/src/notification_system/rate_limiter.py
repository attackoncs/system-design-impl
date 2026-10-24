"""Sliding window rate limiter for the notification system.

Enforces per-user, per-channel and global rate limits using sorted timestamp
lists with pruning. Supports injectable clock for testing.
"""

from __future__ import annotations

import collections
import time
from typing import Callable, Optional

import notification_system.config
import notification_system.exceptions
import notification_system.models


class SlidingWindowRateLimiter:
    """Sliding window rate limiter for per-user, per-channel frequency caps.

    Uses a sorted list of timestamps per (user, channel) pair. On each check,
    removes expired timestamps outside the window, then checks if the count
    exceeds the limit.

    Supports both per-channel and global (cross-channel) rate limits.
    """

    def __init__(
        self,
        per_channel_limits: dict[
            notification_system.models.Channel,
            notification_system.config.RateLimitConfig,
        ],
        global_limit: notification_system.config.RateLimitConfig,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._per_channel_limits = per_channel_limits
        self._global_limit = global_limit
        self._clock = clock or time.monotonic
        # (user_id, channel) -> list of timestamps
        self._channel_windows: dict[
            tuple[str, notification_system.models.Channel], collections.deque[float]
        ] = collections.defaultdict(collections.deque)
        # user_id -> list of timestamps (global across channels)
        self._global_windows: dict[str, collections.deque[float]] = (
            collections.defaultdict(collections.deque)
        )

    def check(self, user_id: str, channel: notification_system.models.Channel) -> None:
        """Check if a notification is allowed under rate limits.

        Args:
            user_id: The recipient user ID.
            channel: The notification channel.

        Raises:
            RateLimitExceededError: If the rate limit is exceeded.
        """
        now = self._clock()

        # Check per-channel limit
        channel_config = self._per_channel_limits.get(channel)
        if channel_config:
            key = (user_id, channel)
            self._prune(self._channel_windows[key], now - channel_config.window_seconds)
            if len(self._channel_windows[key]) >= channel_config.max_count:
                raise notification_system.exceptions.RateLimitExceededError(
                    user_id,
                    channel.value,
                    channel_config.max_count,
                    channel_config.window_seconds,
                )

        # Check global limit
        self._prune(
            self._global_windows[user_id],
            now - self._global_limit.window_seconds,
        )
        if len(self._global_windows[user_id]) >= self._global_limit.max_count:
            raise notification_system.exceptions.RateLimitExceededError(
                user_id,
                "global",
                self._global_limit.max_count,
                self._global_limit.window_seconds,
            )

    def record(self, user_id: str, channel: notification_system.models.Channel) -> None:
        """Record a notification send for rate tracking.

        Args:
            user_id: The recipient user ID.
            channel: The notification channel.
        """
        now = self._clock()
        self._channel_windows[(user_id, channel)].append(now)
        self._global_windows[user_id].append(now)

    def _prune(self, timestamps: collections.deque[float], cutoff: float) -> None:
        """Remove timestamps older than the cutoff."""
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
