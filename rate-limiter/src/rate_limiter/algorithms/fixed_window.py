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

-- TTL for storage keys: window duration plus buffer
local ttl = window + 1

-- Check if request is allowed
if counter < limit then
    counter = counter + 1
    -- Update storage
    redis.call('SET', KEYS[1], tostring(window_start))
    redis.call('EXPIRE', KEYS[1], ttl)
    redis.call('SET', KEYS[2], tostring(counter))
    redis.call('EXPIRE', KEYS[2], ttl)

    local remaining = limit - counter
    -- Time until current window resets
    local reset_after = window_start + window - now

    return {1, remaining, limit, tostring(reset_after), tostring(0)}
else
    -- Denied — calculate retry_after
    redis.call('SET', KEYS[1], tostring(window_start))
    redis.call('EXPIRE', KEYS[1], ttl)
    redis.call('SET', KEYS[2], tostring(counter))
    redis.call('EXPIRE', KEYS[2], ttl)

    -- Time until next window starts
    local retry_after = window_start + window - now
    local reset_after = window_start + window - now

    return {0, 0, limit, tostring(reset_after), tostring(retry_after)}
end
"""


def _memory_fixed_window_script(
    keys: list[str], args: list[str], storage: Any
) -> list[Any]:
    """Atomic fixed window counter logic for the in-memory backend.

    This callable is passed to MemoryStorage.execute_atomic and runs
    under the storage's threading lock for atomicity.

    Args:
        keys: [window_start_key, counter_key]
        args: [window, limit, current_time]
        storage: The MemoryStorage instance (use get_unlocked/set_unlocked).

    Returns:
        A list: [allowed (1 or 0), remaining (int), limit (int), reset_after
        (float), retry_after (float)]
    """
    window_start_key = keys[0]
    counter_key = keys[1]

    window = int(args[0])
    limit = int(args[1])
    now = float(args[2])

    # Get current state from storage
    raw_window_start = storage.get_unlocked(window_start_key)
    raw_counter = storage.get_unlocked(counter_key)

    # Calculate current window start
    current_window = math.floor(now / window) * window

    # Initialize if first request
    if raw_window_start is None:
        window_start = current_window
    else:
        window_start = float(raw_window_start)

    if raw_counter is None:
        counter = 0
    else:
        counter = int(raw_counter)

    # Reset if we've moved to a new window
    if window_start != current_window:
        counter = 0
        window_start = current_window

    # TTL for storage keys: window duration plus buffer
    ttl = window + 1

    # Check if request is allowed
    if counter < limit:
        counter += 1
        # Update storage
        storage.set_unlocked(window_start_key, str(window_start), ttl)
        storage.set_unlocked(counter_key, str(counter), ttl)

        remaining = limit - counter
        # Time until current window resets
        reset_after = window_start + window - now

        return [1, remaining, limit, reset_after, 0.0]
    else:
        # Denied — calculate retry_after
        storage.set_unlocked(window_start_key, str(window_start), ttl)
        storage.set_unlocked(counter_key, str(counter), ttl)

        # Time until next window starts
        retry_after = window_start + window - now
        reset_after = window_start + window - now

        return [0, 0, limit, reset_after, retry_after]


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
        """Check if a request should be allowed under the fixed window
        algorithm.

        Args:
            key: Unique identifier for the rate limit subject.
            rule: The rate limiting rule to apply.
            storage: Storage backend to use for state.

        Returns:
            RateLimitResult indicating if the request is allowed
            and associated metadata.
        """
        window = rule.window
        limit = rule.limit
        now = time.time()

        # Include rule parameters in the key to ensure different rules are
        # isolated
        rule_suffix = f":{window}:{limit}"
        window_start_key = f"{key}{rule_suffix}:window_start"
        counter_key = f"{key}{rule_suffix}:counter"

        keys = [window_start_key, counter_key]
        args = [str(window), str(limit), str(now)]

        result = storage.execute_algorithm(
            LUA_SCRIPT, _memory_fixed_window_script, keys, args
        )

        return algorithms_base.RateLimitResult.from_storage(result)
