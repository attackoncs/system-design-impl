"""Sliding Window Counter rate limiting algorithm implementation."""

from __future__ import annotations

import math
import time
from typing import Any

from rate_limiter import config as rate_limiter_config
from rate_limiter.algorithms import base as algorithms_base
from rate_limiter.storage import base as storage_base

# Redis Lua script for atomic sliding window counter operations.
# KEYS[1] = key_prefix (used to derive window-specific keys)
# ARGV[1] = limit, ARGV[2] = window, ARGV[3] = current_time
# Returns: {allowed (0/1), remaining, limit, reset_after, retry_after}
LUA_SCRIPT = (
    "\n"
    "local limit = tonumber(ARGV[1])\n"
    "local window = tonumber(ARGV[2])\n"
    "local now = tonumber(ARGV[3])\n"
    "\n"
    "-- Calculate current and previous window start times\n"
    "local current_window_start = math.floor(now / window) * window\n"
    "local previous_window_start = current_window_start - window\n"
    "\n"
    "-- Build window-specific keys\n"
    "local current_key = KEYS[1] .. ':' .. tostring(current_window_start)\n"
    "local previous_key = KEYS[1] .. ':' .. tostring(previous_window_start)\n"
    "\n"
    "-- Get counts from both windows\n"
    "local raw_current_count = redis.call('GET', current_key)\n"
    "local raw_previous_count = redis.call('GET', previous_key)\n"
    "\n"
    "local current_count = 0\n"
    "local previous_count = 0\n"
    "\n"
    "if raw_current_count ~= false then\n"
    "    current_count = tonumber(raw_current_count)\n"
    "end\n"
    "\n"
    "if raw_previous_count ~= false then\n"
    "    previous_count = tonumber(raw_previous_count)\n"
    "end\n"
    "\n"
    "-- Calculate overlap percentage of previous window\n"
    "local window_progress = (now - current_window_start) / window\n"
    "local overlap_ratio = 1.0 - window_progress\n"
    "\n"
    "-- Calculate sliding window count with weighted average\n"
    "local sliding_count = current_count + (previous_count * overlap_ratio)\n"
    "\n"
    "-- Check if request can be allowed\n"
    "if sliding_count < limit then\n"
    "    -- Allow request and increment current window\n"
    "    current_count = current_count + 1\n"
    "\n"
    "    -- Set TTL for current window key (2 * window to cover next window's "
    "lookback)\n"
    "    local current_ttl = math.ceil(2 * window)\n"
    "    redis.call('SET', current_key, tostring(current_count))\n"
    "    redis.call('EXPIRE', current_key, current_ttl)\n"
    "\n"
    "    local remaining = limit - math.floor(sliding_count) - 1\n"
    "    if remaining < 0 then remaining = 0 end\n"
    "\n"
    "    -- Reset after is time until current window expires\n"
    "    local reset_after = current_window_start + window - now\n"
    "\n"
    "    return {1, remaining, limit, tostring(reset_after), tostring(0)}\n"
    "else\n"
    "    -- Deny request\n"
    "\n"
    "    -- Calculate when the sliding window will allow requests again\n"
    "    local reset_after = current_window_start + window - now\n"
    "    local retry_after = reset_after\n"
    "\n"
    "    return {0, 0, limit, tostring(reset_after), tostring(retry_after)}\n"
    "end\n"
)


def _memory_sliding_window_counter_script(
    keys: list[str], args: list[str], storage: Any
) -> list[Any]:
    """Atomic sliding window counter logic for the in-memory backend.

    Uses window-start-time-based keys so that when the window advances,
    the old current window naturally becomes the previous window.

    Args:
        keys: [key_prefix]
        args: [limit, window, current_time]
        storage: The MemoryStorage instance (use _get_raw/_set_raw).

    Returns:
        A list: [allowed (1 or 0), remaining (int), limit (int), reset_after
        (float), retry_after (float)]
    """
    key_prefix = keys[0]

    limit = int(args[0])
    window = int(args[1])
    now = float(args[2])

    # Calculate current and previous window start times
    current_window_start = math.floor(now / window) * window
    previous_window_start = current_window_start - window

    # Build window-specific keys
    current_key = f"{key_prefix}:{current_window_start}"
    previous_key = f"{key_prefix}:{previous_window_start}"

    # Get counts from both windows
    raw_current_count = storage.get_unlocked(current_key)
    raw_previous_count = storage.get_unlocked(previous_key)

    current_count = (
        int(raw_current_count) if raw_current_count is not None else 0
    )
    previous_count = (
        int(raw_previous_count) if raw_previous_count is not None else 0
    )

    # Calculate overlap percentage of previous window
    window_progress = (now - current_window_start) / window
    overlap_ratio = 1.0 - window_progress

    # Calculate sliding window count with weighted average
    sliding_count = current_count + (previous_count * overlap_ratio)

    # Check if request can be allowed
    if sliding_count < limit:
        # Allow request and increment current window
        current_count += 1

        # Set TTL for current window key (2 * window to cover next window's
        # lookback)
        current_ttl = int(2 * window)
        storage.set_unlocked(current_key, str(current_count), current_ttl)

        remaining = limit - int(sliding_count) - 1
        if remaining < 0:
            remaining = 0

        # Reset after is time until current window expires
        reset_after = current_window_start + window - now

        return [1, remaining, limit, reset_after, 0.0]
    else:
        # Deny request

        # Calculate when the sliding window will allow requests again
        reset_after = current_window_start + window - now
        retry_after = reset_after

        return [0, 0, limit, reset_after, retry_after]


class SlidingWindowCounterAlgorithm(algorithms_base.BaseAlgorithm):
    """Sliding Window Counter rate limiting algorithm.

    The sliding window counter algorithm combines counts from the current
    window and the previous window using a weighted average. This provides
    smoother rate limiting than fixed windows while being more memory-efficient
    than sliding window log.

    Formula: sliding_count = current_count + (previous_count * overlap_ratio)
    Where overlap_ratio = 1 - (time_elapsed_in_current_window / window_size)

    - Window size = rule.window seconds
    - State stored: per-window counters keyed by window start time
    - More memory-efficient than sliding window log
    - Smoother than fixed window (avoids 2x burst at boundaries)
    - Approximation: assumes requests in previous window are evenly distributed
    """

    def check(
        self,
        key: str,
        rule: rate_limiter_config.RateLimitRule,
        storage: storage_base.BaseStorage,
    ) -> algorithms_base.RateLimitResult:
        """Check if a request should be allowed under the sliding window
        counter algorithm.

        Args:
            key: Unique identifier for the rate limit subject.
            rule: The rate limiting rule to apply.
            storage: Storage backend to use for state.

        Returns:
            RateLimitResult indicating if the request is allowed
            and associated metadata.
        """
        now = time.time()

        # Use a single key prefix; window-specific keys are derived inside the
        # script
        rule_suffix = f":{rule.window}:{rule.limit}"
        key_prefix = f"{key}{rule_suffix}:swc"

        keys = [key_prefix]
        args = [str(rule.limit), str(rule.window), str(now)]

        # Detect storage type and use appropriate script
        if hasattr(storage, "_get_raw"):
            # In-memory backend: use callable
            result = storage.execute_atomic(
                _memory_sliding_window_counter_script, keys, args
            )
        else:
            # Redis backend: use Lua script
            result = storage.execute_atomic(LUA_SCRIPT, keys, args)

        allowed = bool(result[0])
        remaining = int(result[1])
        limit = int(result[2])
        reset_after = float(result[3])
        retry_after = float(result[4]) if not allowed else None

        return algorithms_base.RateLimitResult(
            allowed=allowed,
            remaining=remaining,
            limit=limit,
            reset_after=reset_after,
            retry_after=retry_after,
        )
