"""Fixed Window Counter rate limiting algorithm implementation."""

from __future__ import annotations

import math
import time
from typing import Any

from rate_limiter import config as rate_limiter_config
from rate_limiter.algorithms import base as algorithms_base
from rate_limiter.storage import base as storage_base

# Redis Lua script for atomic fixed window counter operations.
# KEYS[1] = window_start_key, KEYS[2] = counter_key
# ARGV[1] = window, ARGV[2] = limit, ARGV[3] = current_time
# Returns: {allowed (0/1), remaining, limit, reset_after, retry_after}
LUA_SCRIPT = """
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

-- Get current state from storage
local raw_window_start = redis.call('GET', KEYS[1])
local raw_counter = redis.call('GET', KEYS[2])

-- Calculate current window start
local current_window = math.floor(now / window) * window

-- Initialize if first request
local window_start
if raw_window_start == false then
    window_start = current_window
else
    window_start = tonumber(raw_window_start)
end

local counter
if raw_counter == false then
    counter = 0
else
    counter = tonumber(raw_counter)
end

-- Reset if we've moved to a new window
if window_start ~= current_window then
    counter = 0
    window_start = current_window
end

local allowed = counter < limit
if allowed then
    counter = counter + 1
end

-- Persist state for both outcomes; TTL is the window plus buffer
local ttl = window + 1
redis.call('SET', KEYS[1], tostring(window_start))
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('SET', KEYS[2], tostring(counter))
redis.call('EXPIRE', KEYS[2], ttl)

-- Time until the current window ends (also the retry delay when denied)
local reset_after = window_start + window - now
if allowed then
    return {1, limit - counter, limit, tostring(reset_after), tostring(0)}
end
return {0, 0, limit, tostring(reset_after), tostring(reset_after)}
"""


def _memory_fixed_window_script(
    keys: list[str], args: list[str], storage: Any
) -> list[Any]:
    """Python twin of LUA_SCRIPT, run by MemoryStorage under its lock.

    Args:
        keys: [window_start_key, counter_key]
        args: [window, limit, current_time, ...]
        storage: The MemoryStorage instance (use get_unlocked/set_unlocked).

    Returns:
        [allowed (1/0), remaining, limit, reset_after, retry_after]
    """
    window_start_key, counter_key = keys[0], keys[1]
    window = int(args[0])
    limit = int(args[1])
    now = float(args[2])

    raw_window_start = storage.get_unlocked(window_start_key)
    raw_counter = storage.get_unlocked(counter_key)
    current_window = math.floor(now / window) * window
    window_start = (
        current_window if raw_window_start is None else float(raw_window_start)
    )
    counter = 0 if raw_counter is None else int(raw_counter)

    # Reset if we've moved to a new window
    if window_start != current_window:
        counter = 0
        window_start = current_window

    allowed = counter < limit
    if allowed:
        counter += 1

    # Persist state for both outcomes; TTL is the window plus buffer
    ttl = window + 1
    storage.set_unlocked(window_start_key, str(window_start), ttl)
    storage.set_unlocked(counter_key, str(counter), ttl)

    # Time until the current window ends (also the retry delay when denied)
    reset_after = window_start + window - now
    if allowed:
        return [1, limit - counter, limit, reset_after, 0.0]
    return [0, 0, limit, reset_after, reset_after]


class FixedWindowAlgorithm(algorithms_base.BaseAlgorithm):
    """Fixed Window Counter rate limiting algorithm.

    The fixed window algorithm divides time into fixed-size windows
    and counts requests within each window. When the window resets,
    the counter resets to zero.

    - Window size = rule.window seconds
    - State stored: {key}:window_start (timestamp) and {key}:counter (count)
    - Resets at window boundaries
    - Simple and efficient, but can allow up to 2x the limit near boundaries
    """

    def check(
        self,
        key: str,
        rule: rate_limiter_config.RateLimitRule,
        storage: storage_base.BaseStorage,
    ) -> algorithms_base.RateLimitResult:
        """Count one request for ``key`` in the current fixed window."""
        # Storage key layout {key}:{window}:{limit}:... is kept stable.
        prefix = f"{key}:{rule.window}:{rule.limit}"
        return self._execute(
            storage,
            LUA_SCRIPT,
            _memory_fixed_window_script,
            [f"{prefix}:window_start", f"{prefix}:counter"],
            [str(rule.window), str(rule.limit), str(time.time())],
            algorithms_base.window_clock_ttl(rule),
        )
