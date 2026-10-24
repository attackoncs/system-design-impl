"""Per-user monotonic sliding-window admission with atomic recording."""

from __future__ import annotations

import collections
import threading
import time
from collections.abc import Callable

from news_feed_system.config import RateLimitConfig
from news_feed_system.exceptions import RateLimitError


class SlidingWindowRateLimiter:
    """Process-local limiter; rejected attempts do not consume capacity."""

    def __init__(
        self, config: RateLimitConfig, clock: Callable[[], float] | None = None
    ) -> None:
        self._config = config
        self._clock = clock or time.monotonic
        self._windows: dict[str, collections.deque[float]] = {}
        self._lock = threading.RLock()
        self._last_cleanup = self._clock()

    def check_and_record(self, user_id: str) -> None:
        with self._lock:
            now = self._clock()
            cutoff = now - self._config.window_seconds
            # Retire inactive users once per window instead of retaining every ID forever.
            if now - self._last_cleanup >= self._config.window_seconds:
                self._windows = {
                    key: values
                    for key, values in self._windows.items()
                    if values and values[-1] > cutoff
                }
                self._last_cleanup = now
            timestamps = self._windows.setdefault(user_id, collections.deque())
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= self._config.max_posts:
                raise RateLimitError(
                    user_id, self._config.max_posts, self._config.window_seconds
                )
            timestamps.append(now)
