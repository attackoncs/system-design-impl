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
LUA_SCRIPT = """
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

-- Calculate current and previous window start times
local current_window_start = math.floor(now / window) * window
local previous_window_start = current_window_start - window

-- Build window-specific keys
local current_key = KEYS[1] .. ':' .. tostring(current_window_start)
local previous_key = KEYS[1] .. ':' .. tostring(previous_window_start)

-- Get counts from both windows
local raw_current_count = redis.call('GET', current_key)
local raw_previous_count = redis.call('GET', previous_key)

local current_count = 0
local previous_count = 0

if raw_current_count ~= false then
    current_count = tonumber(raw_current_count)
end

if raw_previous_count ~= false then
    previous_count = tonumber(raw_previous_count)
end

-- Calculate overlap percentage of previous window
local window_progress = (now - current_window_start) / window
local overlap_ratio = 1.0 - window_progress

-- Calculate sliding window count with weighted average
local sliding_count = current_count + (previous_count * overlap_ratio)

-- Time until the current window ends (also the retry delay when denied)
local reset_after = current_window_start + window - now

if sliding_count >= limit then
    return {0, 0, limit, tostring(reset_after), tostring(reset_after)}
end

-- Allow request and increment current window; TTL of two windows covers
-- the next window's lookback
current_count = current_count + 1
redis.call('SET', current_key, tostring(current_count))
redis.call('EXPIRE', current_key, math.ceil(2 * window))

local remaining = limit - math.floor(sliding_count) - 1
if remaining < 0 then remaining = 0 end

return {1, remaining, limit, tostring(reset_after), tostring(0)}
"""


def _memory_sliding_window_counter_script(
    keys: list[str], args: list[str], storage: Any
) -> list[Any]:
    """Python twin of LUA_SCRIPT, run by MemoryStorage under its lock.

    Uses window-start-time-based keys so that when the window advances,
    the old current window naturally becomes the previous window.

    Args:
        keys: [key_prefix]
        args: [limit, window, current_time, ...]
        storage: The MemoryStorage instance (use get_unlocked/set_unlocked).

    Returns:
        [allowed (1/0), remaining, limit, reset_after, retry_after]
    """
    key_prefix = keys[0]
    limit = int(args[0])
    window = int(args[1])
    now = float(args[2])

    current_window_start = math.floor(now / window) * window
    previous_window_start = current_window_start - window
    current_key = f"{key_prefix}:{current_window_start}"
    previous_key = f"{key_prefix}:{previous_window_start}"

    raw_current_count = storage.get_unlocked(current_key)
    raw_previous_count = storage.get_unlocked(previous_key)
    current_count = int(raw_current_count) if raw_current_count is not None else 0
    previous_count = int(raw_previous_count) if raw_previous_count is not None else 0

    # Weight the previous window by its overlap with the sliding window
    overlap_ratio = 1.0 - (now - current_window_start) / window
    sliding_count = current_count + (previous_count * overlap_ratio)

    # Time until the current window ends (also the retry delay when denied)
    reset_after = current_window_start + window - now
    if sliding_count >= limit:
        return [0, 0, limit, reset_after, reset_after]

    # TTL of two windows covers the next window's lookback
    storage.set_unlocked(current_key, str(current_count + 1), int(2 * window))
    remaining = max(0, limit - int(sliding_count) - 1)
    return [1, remaining, limit, reset_after, 0.0]


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
        """Count one request for ``key`` against the weighted sliding window."""
        # Window-specific keys are derived inside the script from this prefix.
        return self._execute(
            storage,
            LUA_SCRIPT,
            _memory_sliding_window_counter_script,
            [f"{key}:{rule.window}:{rule.limit}:swc"],
            [str(rule.limit), str(rule.window), str(time.time())],
            algorithms_base.window_clock_ttl(rule),
        )
