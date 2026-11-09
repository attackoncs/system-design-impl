"""Leaking Bucket rate limiting algorithm implementation."""

from __future__ import annotations

import time
from typing import Any

from rate_limiter import config as rate_limiter_config
from rate_limiter.algorithms import base as algorithms_base
from rate_limiter.storage import base as storage_base

# Redis Lua script for atomic leaking bucket operations.
# KEYS[1] = queue_count_key, KEYS[2] = last_drain_key
# ARGV[1] = capacity, ARGV[2] = drain_rate, ARGV[3] = current_time
# Returns: {allowed (0/1), remaining, limit, reset_after, retry_after}
LUA_SCRIPT = """
local capacity = tonumber(ARGV[1])
local drain_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

-- Get current queue count (default to 0 if nil)
local raw_queue_count = redis.call('GET', KEYS[1])
local queue_count
if raw_queue_count == false then
    queue_count = 0
else
    queue_count = tonumber(raw_queue_count)
end

-- Get last drain time (default to current_time if nil)
local raw_last_drain = redis.call('GET', KEYS[2])
local last_drain
if raw_last_drain == false then
    last_drain = now
else
    last_drain = tonumber(raw_last_drain)
end

-- Calculate elapsed time and drain the queue
local elapsed = now - last_drain
if elapsed > 0 and drain_rate > 0 then
    queue_count = math.max(0, queue_count - elapsed * drain_rate)
end

-- A whole request must fit
local allowed = queue_count + 1 <= capacity
if allowed then
    queue_count = queue_count + 1
end

-- Persist state for both outcomes; TTL covers draining a full queue plus buffer
local ttl = math.ceil(capacity / drain_rate) + 1
redis.call('SET', KEYS[1], tostring(queue_count))
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('SET', KEYS[2], tostring(now))
redis.call('EXPIRE', KEYS[2], ttl)

local reset_after = queue_count / drain_rate
if allowed then
    local remaining = math.floor(capacity - queue_count)
    return {1, remaining, capacity, tostring(reset_after), tostring(0)}
end
local retry_after = (queue_count - capacity + 1) / drain_rate
return {0, 0, capacity, tostring(reset_after), tostring(retry_after)}
"""


def _memory_leaking_bucket_script(
    keys: list[str], args: list[str], storage: Any
) -> list[Any]:
    """Python twin of LUA_SCRIPT, run by MemoryStorage under its lock.

    Args:
        keys: [queue_count_key, last_drain_key]
        args: [capacity, drain_rate, current_time, ...]
        storage: The MemoryStorage instance (use get_unlocked/set_unlocked).

    Returns:
        [allowed (1/0), remaining, limit, reset_after, retry_after]
    """
    queue_count_key, last_drain_key = keys[0], keys[1]
    capacity = float(args[0])
    drain_rate = float(args[1])
    now = float(args[2])

    raw_queue_count = storage.get_unlocked(queue_count_key)
    raw_last_drain = storage.get_unlocked(last_drain_key)
    queue_count = 0.0 if raw_queue_count is None else float(raw_queue_count)
    last_drain = now if raw_last_drain is None else float(raw_last_drain)

    # Drain virtual occupancy based on elapsed time
    elapsed = now - last_drain
    if elapsed > 0 and drain_rate > 0:
        queue_count = max(0.0, queue_count - elapsed * drain_rate)

    # A whole request must fit
    allowed = queue_count + 1 <= capacity
    if allowed:
        queue_count += 1

    # Persist state for both outcomes; TTL covers draining a full queue
    ttl = int(capacity / drain_rate) + 1 if drain_rate > 0 else None
    storage.set_unlocked(queue_count_key, str(queue_count), ttl)
    storage.set_unlocked(last_drain_key, str(now), ttl)

    # Time until the queue is fully drained
    reset_after = queue_count / drain_rate if drain_rate > 0 else 0.0
    if allowed:
        return [1, int(capacity - queue_count), int(capacity), reset_after, 0.0]
    # Time until enough space drains for one whole request
    retry_after = (queue_count - capacity + 1) / drain_rate if drain_rate > 0 else 0.0
    return [0, 0, int(capacity), reset_after, retry_after]


class LeakingBucketAlgorithm(algorithms_base.BaseAlgorithm):
    """Leaking Bucket rate limiting algorithm.

    The leaking bucket algorithm tracks virtual occupancy draining at a
    fixed rate. A whole request must fit before admission. Execution pacing
    is provided separately by LeakyBucketQueue.

    - Queue capacity = rule.capacity or rule.limit
    - Drain rate = rule.limit / rule.window requests per second
    - Admission-only virtual occupancy; no execution queue or pacing
    """

    def check(
        self,
        key: str,
        rule: rate_limiter_config.RateLimitRule,
        storage: storage_base.BaseStorage,
    ) -> algorithms_base.RateLimitResult:
        """Admit one request for ``key`` if it fits in the virtual queue."""
        capacity, drain_rate, clock_ttl = algorithms_base.bucket_parameters(rule)
        return self._execute(
            storage,
            LUA_SCRIPT,
            _memory_leaking_bucket_script,
            [f"{key}:queue_count", f"{key}:last_drain"],
            [str(capacity), str(drain_rate), str(time.time())],
            clock_ttl,
        )
