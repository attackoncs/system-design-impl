"""Token Bucket rate limiting algorithm implementation."""

from __future__ import annotations

import time
from typing import Any

from rate_limiter import config as rate_limiter_config
from rate_limiter.algorithms import base as algorithms_base
from rate_limiter.storage import base as storage_base

# Redis Lua script for atomic token bucket operations.
# KEYS[1] = tokens_key, KEYS[2] = last_refill_key
# ARGV[1] = capacity, ARGV[2] = refill_rate, ARGV[3] = current_time
# Returns: {allowed (0/1), remaining, limit, reset_after, retry_after}
LUA_SCRIPT = """
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

-- Get current tokens (default to capacity if nil)
local raw_tokens = redis.call('GET', KEYS[1])
local tokens
if raw_tokens == false then
    tokens = capacity
else
    tokens = tonumber(raw_tokens)
end

-- Get last refill time (default to current_time if nil)
local raw_last_refill = redis.call('GET', KEYS[2])
local last_refill
if raw_last_refill == false then
    last_refill = now
else
    last_refill = tonumber(raw_last_refill)
end

-- Calculate refilled tokens based on elapsed time
local elapsed = now - last_refill
if elapsed > 0 then
    tokens = math.min(capacity, tokens + elapsed * refill_rate)
end

-- Attempt to consume one token
local allowed = tokens >= 1
if allowed then
    tokens = tokens - 1
end

-- Persist state for both outcomes; TTL covers a full refill cycle plus buffer
local ttl = math.ceil(capacity / refill_rate) + 1
redis.call('SET', KEYS[1], tostring(tokens))
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('SET', KEYS[2], tostring(now))
redis.call('EXPIRE', KEYS[2], ttl)

local reset_after = (capacity - tokens) / refill_rate
if allowed then
    return {1, math.floor(tokens), capacity, tostring(reset_after), tostring(0)}
end
local retry_after = (1.0 - tokens) / refill_rate
return {0, 0, capacity, tostring(reset_after), tostring(retry_after)}
"""


def _memory_token_bucket_script(
    keys: list[str], args: list[str], storage: Any
) -> list[Any]:
    """Python twin of LUA_SCRIPT, run by MemoryStorage under its lock.

    Args:
        keys: [tokens_key, last_refill_key]
        args: [capacity, refill_rate, current_time, ...]
        storage: The MemoryStorage instance (use get_unlocked/set_unlocked).

    Returns:
        [allowed (1/0), remaining, limit, reset_after, retry_after]
    """
    tokens_key, last_refill_key = keys[0], keys[1]
    capacity = float(args[0])
    refill_rate = float(args[1])
    now = float(args[2])

    raw_tokens = storage.get_unlocked(tokens_key)
    raw_last_refill = storage.get_unlocked(last_refill_key)
    tokens = capacity if raw_tokens is None else float(raw_tokens)
    last_refill = now if raw_last_refill is None else float(raw_last_refill)

    # Refill based on elapsed time
    elapsed = now - last_refill
    if elapsed > 0:
        tokens = min(capacity, tokens + elapsed * refill_rate)

    # Attempt to consume one token
    allowed = tokens >= 1
    if allowed:
        tokens -= 1

    # Persist state for both outcomes; TTL covers a full refill cycle
    ttl = int(capacity / refill_rate) + 1 if refill_rate > 0 else None
    storage.set_unlocked(tokens_key, str(tokens), ttl)
    storage.set_unlocked(last_refill_key, str(now), ttl)

    # Time until the bucket is full again
    reset_after = (capacity - tokens) / refill_rate if refill_rate > 0 else 0.0
    if allowed:
        return [1, int(tokens), int(capacity), reset_after, 0.0]
    # Time until at least one token is refilled
    retry_after = (1.0 - tokens) / refill_rate if refill_rate > 0 else 0.0
    return [0, 0, int(capacity), reset_after, retry_after]


class TokenBucketAlgorithm(algorithms_base.BaseAlgorithm):
    """Token Bucket rate limiting algorithm.

    The token bucket algorithm works by maintaining a bucket of tokens
    that refills at a steady rate. Each request consumes one token.
    If the bucket is empty, the request is denied.

    - Bucket capacity = rule.capacity or rule.limit
    - Refill rate = rule.limit / rule.window tokens per second
    - Allows bursts up to the bucket capacity
    """

    def check(
        self,
        key: str,
        rule: rate_limiter_config.RateLimitRule,
        storage: storage_base.BaseStorage,
    ) -> algorithms_base.RateLimitResult:
        """Consume one token for ``key`` if available."""
        capacity, refill_rate, clock_ttl = algorithms_base.bucket_parameters(rule)
        return self._execute(
            storage,
            LUA_SCRIPT,
            _memory_token_bucket_script,
            [f"{key}:tokens", f"{key}:last_refill"],
            [str(capacity), str(refill_rate), str(time.time())],
            clock_ttl,
        )
