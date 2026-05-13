"""Sliding Window Log rate limiting algorithm implementation."""

from __future__ import annotations

import time
from typing import Any

from ..config import RateLimitRule
from ..storage.base import BaseStorage
from .base import BaseAlgorithm, RateLimitResult

# Redis Lua script for atomic sliding window log operations.
# KEYS[1] = log_key (sorted set of timestamps)
# ARGV[1] = limit, ARGV[2] = window, ARGV[3] = current_time, ARGV[4] = request_id
# Returns: {allowed (0/1), remaining, limit, reset_after, retry_after}
LUA_SCRIPT = """
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local request_id = ARGV[4]

-- Remove expired entries (older than window)
local cutoff = now - window
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)

-- Count current entries
local count = redis.call('ZCARD', KEYS[1])

-- Check if we can add this request
if count < limit then
    -- Allow request and add timestamp to log
    redis.call('ZADD', KEYS[1], now, request_id)

    -- Set TTL to ensure cleanup (window + 1 second buffer)
    redis.call('EXPIRE', KEYS[1], math.ceil(window) + 1)

    local remaining = limit - count - 1

    -- Find oldest entry to calculate reset time
    -- ZRANGEBYSCORE returns flat array: [member1, score1, member2, score2, ...]
    local oldest_entries = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    local reset_after
    if #oldest_entries >= 2 then
        local oldest_time = tonumber(oldest_entries[2])
        reset_after = oldest_time + window - now
    else
        reset_after = window
    end

    return {1, remaining, limit, tostring(reset_after), tostring(0)}
else
    -- Deny request

    -- Find oldest entry to calculate retry time
    local oldest_entries = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    local retry_after
    if #oldest_entries >= 2 then
        local oldest_time = tonumber(oldest_entries[2])
        retry_after = oldest_time + window - now
    else
        retry_after = window
    end

    -- Set TTL to ensure cleanup
    redis.call('EXPIRE', KEYS[1], math.ceil(window) + 1)

    return {0, 0, limit, tostring(retry_after), tostring(retry_after)}
end
"""


def _memory_sliding_window_log_script(
    keys: list[str], args: list[str], storage: Any
) -> list[Any]:
    """Atomic sliding window log logic for the in-memory backend.

    This callable is passed to MemoryStorage.execute_atomic and runs
    under the storage's threading lock for atomicity.

    Args:
        keys: [log_key]
        args: [limit, window, current_time, request_id]
        storage: The MemoryStorage instance (use _get_raw/_set_raw).

    Returns:
        A list: [allowed (1 or 0), remaining (int), limit (int), reset_after (float), retry_after (float)]
    """
    log_key = keys[0]

    limit = int(args[0])
    window = int(args[1])
    now = float(args[2])
    request_id = args[3]

    # Get current log from storage
    raw_log = storage._get_raw(log_key)

    if raw_log is None:
        log_entries = []
    else:
        # Parse stored log entries (format: "timestamp1,id1;timestamp2,id2;...")
        log_entries = []
        if raw_log:
            for entry in raw_log.split(';'):
                if entry:
                    parts = entry.split(',')
                    if len(parts) == 2:
                        timestamp, entry_id = float(parts[0]), parts[1]
                        log_entries.append((timestamp, entry_id))

    # Remove expired entries (older than window)
    cutoff = now - window
    current_entries = [(ts, entry_id) for ts, entry_id in log_entries if ts > cutoff]

    # Check if we can add this request
    if len(current_entries) < limit:
        # Allow request and add timestamp to log
        current_entries.append((now, request_id))

        # Store updated log
        log_str = ';'.join([f"{ts},{entry_id}" for ts, entry_id in current_entries])
        ttl = window + 1
        storage._set_raw(log_key, log_str, ttl)

        remaining = limit - len(current_entries)

        # Calculate reset time (when oldest entry expires)
        if current_entries:
            oldest_time = min(ts for ts, _ in current_entries)
            reset_after = oldest_time + window - now
        else:
            reset_after = window

        return [1, remaining, limit, reset_after, 0.0]
    else:
        # Deny request

        # Calculate retry time (when oldest entry expires)
        if current_entries:
            oldest_time = min(ts for ts, _ in current_entries)
            retry_after = oldest_time + window - now
        else:
            retry_after = window

        # Store log (even if denied, to maintain TTL)
        log_str = ';'.join([f"{ts},{entry_id}" for ts, entry_id in current_entries])
        ttl = window + 1
        storage._set_raw(log_key, log_str, ttl)

        return [0, 0, limit, retry_after, retry_after]


class SlidingWindowLogAlgorithm(BaseAlgorithm):
    """Sliding Window Log rate limiting algorithm.

    The sliding window log algorithm maintains a log of all request timestamps
    within the current window. It provides precise rate limiting but uses more
    memory than other algorithms as it stores individual request timestamps.

    - Window size = rule.window seconds
    - Maximum requests per window = rule.limit
    - State stored: sorted set of request timestamps
    - Most accurate algorithm, but higher memory usage
    - Automatically expires old entries
    """

    def check(
        self, key: str, rule: RateLimitRule, storage: BaseStorage
    ) -> RateLimitResult:
        """Check if a request should be allowed under the sliding window log algorithm.

        Args:
            key: Unique identifier for the rate limit subject.
            rule: The rate limiting rule to apply.
            storage: Storage backend to use for state.

        Returns:
            RateLimitResult indicating if the request is allowed
            and associated metadata.
        """
        now = time.time()

        # Generate unique request ID for this request
        request_id = f"{now}:{id(self)}"

        # Include rule parameters in the key to ensure different rules are isolated
        rule_suffix = f":{rule.window}:{rule.limit}"
        log_key = f"{key}{rule_suffix}:log"

        keys = [log_key]
        args = [str(rule.limit), str(rule.window), str(now), request_id]

        # Detect storage type and use appropriate script
        if hasattr(storage, "_get_raw"):
            # In-memory backend: use callable
            result = storage.execute_atomic(
                _memory_sliding_window_log_script, keys, args
            )
        else:
            # Redis backend: use Lua script
            result = storage.execute_atomic(LUA_SCRIPT, keys, args)

        allowed = bool(result[0])
        remaining = int(result[1])
        limit = int(result[2])
        reset_after = float(result[3])
        retry_after = float(result[4]) if not allowed else None

        return RateLimitResult(
            allowed=allowed,
            remaining=remaining,
            limit=limit,
            reset_after=reset_after,
            retry_after=retry_after,
        )
