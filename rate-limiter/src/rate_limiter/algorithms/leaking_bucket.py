"""Leaking Bucket rate limiting algorithm implementation."""

from __future__ import annotations

import time
from typing import Any

from ..config import RateLimitRule
from ..storage.base import BaseStorage
from .base import BaseAlgorithm, RateLimitResult

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

-- TTL for storage keys: enough time to drain a full queue plus buffer
local ttl = math.ceil(capacity / drain_rate) + 1

-- Attempt to add to queue
if queue_count < capacity then
    queue_count = queue_count + 1
    redis.call('SET', KEYS[1], tostring(queue_count))
    redis.call('EXPIRE', KEYS[1], ttl)
    redis.call('SET', KEYS[2], tostring(now))
    redis.call('EXPIRE', KEYS[2], ttl)

    local remaining = math.floor(capacity - queue_count)
    local reset_after = queue_count / drain_rate

    return {1, remaining, capacity, tostring(reset_after), tostring(0)}
else
    redis.call('SET', KEYS[1], tostring(queue_count))
    redis.call('EXPIRE', KEYS[1], ttl)
    redis.call('SET', KEYS[2], tostring(now))
    redis.call('EXPIRE', KEYS[2], ttl)

    local retry_after = 1 / drain_rate
    local reset_after = queue_count / drain_rate

    return {0, 0, capacity, tostring(reset_after), tostring(retry_after)}
end
"""


def _memory_leaking_bucket_script(
    keys: list[str], args: list[str], storage: Any
) -> list[Any]:
    """Atomic leaking bucket logic for the in-memory backend.

    This callable is passed to MemoryStorage.execute_atomic and runs
    under the storage's threading lock for atomicity.

    Args:
        keys: [queue_count_key, last_drain_key]
        args: [capacity, drain_rate, current_time]
        storage: The MemoryStorage instance (use _get_raw/_set_raw).

    Returns:
        A list: [allowed (1 or 0), remaining (int), limit (int), reset_after (float), retry_after (float)]
    """
    queue_count_key = keys[0]
    last_drain_key = keys[1]

    capacity = float(args[0])
    drain_rate = float(args[1])
    now = float(args[2])

    # Get current state from storage
    raw_queue_count = storage._get_raw(queue_count_key)
    raw_last_drain = storage._get_raw(last_drain_key)

    # Initialize if first request
    if raw_queue_count is None:
        queue_count = 0.0
    else:
        queue_count = float(raw_queue_count)

    if raw_last_drain is None:
        last_drain = now
    else:
        last_drain = float(raw_last_drain)

    # Calculate elapsed time and drain the queue
    elapsed = now - last_drain
    if elapsed > 0 and drain_rate > 0:
        queue_count = max(0.0, queue_count - elapsed * drain_rate)

    # TTL for storage keys: enough time to drain a full queue plus buffer
    ttl = int(capacity / drain_rate) + 1 if drain_rate > 0 else None

    # Attempt to add to queue
    if queue_count < capacity:
        queue_count += 1
        # Update storage
        storage._set_raw(queue_count_key, str(queue_count), ttl)
        storage._set_raw(last_drain_key, str(now), ttl)

        remaining = int(capacity - queue_count)
        # Time until queue is fully drained
        reset_after = queue_count / drain_rate if drain_rate > 0 else 0.0

        return [1, remaining, int(capacity), reset_after, 0.0]
    else:
        # Queue is full — calculate time until one item drains
        storage._set_raw(queue_count_key, str(queue_count), ttl)
        storage._set_raw(last_drain_key, str(now), ttl)

        retry_after = 1.0 / drain_rate if drain_rate > 0 else 0.0
        reset_after = queue_count / drain_rate if drain_rate > 0 else 0.0

        return [0, 0, int(capacity), reset_after, retry_after]


class LeakingBucketAlgorithm(BaseAlgorithm):
    """Leaking Bucket rate limiting algorithm.

    The leaking bucket algorithm maintains a queue that drains at a
    fixed rate. Incoming requests are added to the queue. If the queue
    is full, the request is denied.

    - Queue capacity = rule.limit
    - Drain rate = rule.limit / rule.window requests per second
    - Provides a smooth, fixed output rate
    """

    def check(
        self, key: str, rule: RateLimitRule, storage: BaseStorage
    ) -> RateLimitResult:
        """Check if a request should be allowed under the leaking bucket algorithm.

        Args:
            key: Unique identifier for the rate limit subject.
            rule: The rate limiting rule to apply.
            storage: Storage backend to use for state.

        Returns:
            RateLimitResult indicating if the request is allowed
            and associated metadata.
        """
        capacity = rule.limit
        drain_rate = rule.limit / rule.window if rule.window > 0 else 0.0
        now = time.time()

        queue_count_key = f"{key}:queue_count"
        last_drain_key = f"{key}:last_drain"

        keys = [queue_count_key, last_drain_key]
        args = [str(capacity), str(drain_rate), str(now)]

        # Detect storage type and use appropriate script
        if hasattr(storage, "_get_raw"):
            # In-memory backend: use callable
            result = storage.execute_atomic(
                _memory_leaking_bucket_script, keys, args
            )
        else:
            # Redis backend: use Lua script (to be implemented in Task 5.2)
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
