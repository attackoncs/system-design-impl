"""Sliding Window Log rate limiting algorithm implementation."""

from __future__ import annotations

import time
import uuid
from typing import Any

from rate_limiter import config as rate_limiter_config
from rate_limiter.algorithms import base as algorithms_base
from rate_limiter.storage import base as storage_base

# Redis Lua script for atomic sliding window log operations.
# KEYS[1] = log_key (sorted set of timestamps)
# ARGV[1] = limit, ARGV[2] = window, ARGV[3] = current_time, ARGV[4] =
# request_id
# Returns: {allowed (0/1), remaining, limit, reset_after, retry_after}
LUA_SCRIPT = """
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local request_id = ARGV[4]

-- Remove expired entries (older than window)
local cutoff = now - window
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)

-- Count current entries; only accepted requests are logged
local count = redis.call('ZCARD', KEYS[1])
local allowed = count < limit
if allowed then
    redis.call('ZADD', KEYS[1], now, request_id)
end

-- Set TTL to ensure cleanup (window + 1 second buffer)
redis.call('EXPIRE', KEYS[1], math.ceil(window) + 1)

-- Time until the oldest logged entry leaves the window
-- ZRANGE WITHSCORES returns a flat array: [member1, score1, ...]
local oldest_entries = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
local until_oldest_expires
if #oldest_entries >= 2 then
    until_oldest_expires = tonumber(oldest_entries[2]) + window - now
else
    until_oldest_expires = window
end

if allowed then
    local remaining = limit - count - 1
    return {1, remaining, limit, tostring(until_oldest_expires), tostring(0)}
end
return {0, 0, limit, tostring(until_oldest_expires), tostring(until_oldest_expires)}
"""


def _parse_log(raw_log: str | None) -> list[tuple[float, str]]:
    """Decode "timestamp,id;timestamp,id;..." into (timestamp, id) pairs."""
    entries = []
    for entry in (raw_log or "").split(";"):
        parts = entry.split(",") if entry else []
        if len(parts) == 2:
            entries.append((float(parts[0]), parts[1]))
    return entries


def _memory_sliding_window_log_script(
    keys: list[str], args: list[str], storage: Any
) -> list[Any]:
    """Python twin of LUA_SCRIPT, run by MemoryStorage under its lock.

    Args:
        keys: [log_key]
        args: [limit, window, current_time, request_id, ...]
        storage: The MemoryStorage instance (use get_unlocked/set_unlocked).

    Returns:
        [allowed (1/0), remaining, limit, reset_after, retry_after]
    """
    log_key = keys[0]
    limit = int(args[0])
    window = int(args[1])
    now = float(args[2])
    request_id = args[3]

    # Drop entries older than the window; only accepted requests are logged
    cutoff = now - window
    entries = [
        (ts, entry_id)
        for ts, entry_id in _parse_log(storage.get_unlocked(log_key))
        if ts > cutoff
    ]
    allowed = len(entries) < limit
    if allowed:
        entries.append((now, request_id))

    # Persist the pruned log for both outcomes to maintain its TTL
    log_str = ";".join(f"{ts},{entry_id}" for ts, entry_id in entries)
    storage.set_unlocked(log_key, log_str, window + 1)

    # Time until the oldest logged entry leaves the window
    if entries:
        until_oldest_expires = min(ts for ts, _ in entries) + window - now
    else:
        until_oldest_expires = window
    if allowed:
        return [1, limit - len(entries), limit, until_oldest_expires, 0.0]
    return [0, 0, limit, until_oldest_expires, until_oldest_expires]


class SlidingWindowLogAlgorithm(algorithms_base.BaseAlgorithm):
    """Sliding Window Log rate limiting algorithm.

    The sliding window log algorithm maintains a log of accepted request
    timestamps within the current window. It provides precise rate limiting
    but uses more memory than other algorithms as it stores individual
    request timestamps.

    - Window size = rule.window seconds
    - Maximum requests per window = rule.limit
    - State stored: sorted set of request timestamps
    - Most accurate algorithm, but higher memory usage
    - Automatically expires old entries
    """

    def check(
        self,
        key: str,
        rule: rate_limiter_config.RateLimitRule,
        storage: storage_base.BaseStorage,
    ) -> algorithms_base.RateLimitResult:
        """Log one accepted request for ``key`` if the window has room."""
        # A unique member keeps same-timestamp requests distinct.
        request_id = uuid.uuid4().hex
        return self._execute(
            storage,
            LUA_SCRIPT,
            _memory_sliding_window_log_script,
            [f"{key}:{rule.window}:{rule.limit}:log"],
            [str(rule.limit), str(rule.window), str(time.time()), request_id],
            algorithms_base.window_clock_ttl(rule),
        )
