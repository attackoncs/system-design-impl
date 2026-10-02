import time
from collections import defaultdict

from news_feed_system.config import RateLimitConfig
from news_feed_system.exceptions import RateLimitError


class SlidingWindowRateLimiter:
    """Sliding window rate limiter for post publishing frequency.

    Tracks timestamps of post publications per user. On each check,
    prunes expired timestamps and verifies the count is within limits.
    """

    def __init__(
        self, config: RateLimitConfig, clock: callable = None
    ) -> None:
        self._config = config
        self._clock = clock or time.monotonic
        self._windows: dict[str, list[float]] = defaultdict(list)

    def check_and_record(self, user_id: str) -> None:
        """Check rate limit and record the publish event.

        Args:
            user_id: The publishing user's ID.

        Raises:
            RateLimitError: If the user has exceeded the rate limit.
        """
        now = self._clock()
        cutoff = now - self._config.window_seconds
        timestamps = self._windows[user_id]

        # Prune expired entries
        while timestamps and timestamps[0] < cutoff:
            timestamps.pop(0)

        if len(timestamps) >= self._config.max_posts:
            raise RateLimitError(
                user_id, self._config.max_posts, self._config.window_seconds
            )

        timestamps.append(now)
