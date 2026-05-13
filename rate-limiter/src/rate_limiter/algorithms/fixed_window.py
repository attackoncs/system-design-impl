"""Fixed Window Counter rate limiting algorithm implementation."""

from __future__ import annotations

import math
import time
from typing import Any

from ..config import RateLimitRule
from ..storage.base import BaseStorage
from .base import BaseAlgorithm, RateLimitResult

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
        storage: The MemoryStorage instance (use _get_raw/_set_raw).

    Returns:
        A list: [allowed (1 or 0), remaining (int), limit (int), reset_after (float), retry_after (float)]
    """
    window_start_key = keys[0]
    counter_key = keys[1]

    window = int(args[0])
    limit = int(args[1])
    now = float(args[2])

    # Get current state from storage
    raw_window_start = storage._get_raw(window_start_key)
    raw_counter = storage._get_raw(counter_key)

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
        storage._set_raw(window_start_key, str(window_start), ttl)
        storage._set_raw(counter_key, str(counter), ttl)

        remaining = limit - counter
        # Time until current window resets
        reset_after = window_start + window - now

        return [1, remaining, limit, reset_after, 0.0]
    else:
        # Denied — calculate retry_after
        storage._set_raw(window_start_key, str(window_start), ttl)
        storage._set_raw(counter_key, str(counter), ttl)

        # Time until next window starts
        retry_after = window_start + window - now
        reset_after = window_start + window - now

        return [0, 0, limit, reset_after, retry_after]


class FixedWindowAlgorithm(BaseAlgorithm):
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
        self, key: str, rule: RateLimitRule, storage: BaseStorage
    ) -> RateLimitResult:
        """Check if a request should be allowed under the fixed window algorithm.

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

        # Include rule parameters in the key to ensure different rules are isolated
        rule_suffix = f":{window}:{limit}"
        window_start_key = f"{key}{rule_suffix}:window_start"
        counter_key = f"{key}{rule_suffix}:counter"

        keys = [window_start_key, counter_key]
        args = [str(window), str(limit), str(now)]

        # Detect storage type and use appropriate script
        if hasattr(storage, "_get_raw"):
            # In-memory backend: use callable
            result = storage.execute_atomic(
                _memory_fixed_window_script, keys, args
            )
        else:
            # Redis backend: use Lua script (to be implemented in Task 6.2)
            result = storage.execute_atomic(LUA_SCRIPT, keys, args)

        allowed = bool(result[0])
        remaining = int(result[1])
        limit_val = int(result[2])
        reset_after = float(result[3])
        retry_after = float(result[4]) if not allowed else None

        return RateLimitResult(
            allowed=allowed,
            remaining=remaining,
            limit=limit_val,
            reset_after=reset_after,
            retry_after=retry_after,
        )
