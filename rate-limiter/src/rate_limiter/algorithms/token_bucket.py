"""Token Bucket rate limiting algorithm implementation."""

from __future__ import annotations

import time
from typing import Any

from ..config import RateLimitRule
from ..storage.base import BaseStorage
from .base import BaseAlgorithm, RateLimitResult

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

-- TTL for storage keys: enough time to cover a full refill cycle plus buffer
local ttl = math.ceil(capacity / refill_rate) + 1

-- Attempt to consume one token
if tokens >= 1 then
    tokens = tokens - 1
    redis.call('SET', KEYS[1], tostring(tokens))
    redis.call('EXPIRE', KEYS[1], ttl)
    redis.call('SET', KEYS[2], tostring(now))
    redis.call('EXPIRE', KEYS[2], ttl)

    local remaining = math.floor(tokens)
    local reset_after = (capacity - tokens) / refill_rate

    return {1, remaining, capacity, tostring(reset_after), tostring(0)}
else
    redis.call('SET', KEYS[1], tostring(tokens))
    redis.call('EXPIRE', KEYS[1], ttl)
    redis.call('SET', KEYS[2], tostring(now))
    redis.call('EXPIRE', KEYS[2], ttl)

    local deficit = 1.0 - tokens
    local retry_after = deficit / refill_rate
    local reset_after = (capacity - tokens) / refill_rate

    return {0, 0, capacity, tostring(reset_after), tostring(retry_after)}
end
"""


def _memory_token_bucket_script(
    keys: list[str], args: list[str], storage: Any
) -> list[Any]:
    """Atomic token bucket logic for the in-memory backend.

    This callable is passed to MemoryStorage.execute_atomic and runs
    under the storage's threading lock for atomicity.

    Args:
        keys: [tokens_key, last_refill_key]
        args: [capacity, refill_rate, current_time]
        storage: The MemoryStorage instance (use _get_raw/_set_raw).

    Returns:
        A list: [allowed (1 or 0), remaining (int), limit (int), reset_after (float), retry_after (float)]
    """
    tokens_key = keys[0]
    last_refill_key = keys[1]

    capacity = float(args[0])
    refill_rate = float(args[1])
    now = float(args[2])

    # Get current state from storage
    raw_tokens = storage._get_raw(tokens_key)
    raw_last_refill = storage._get_raw(last_refill_key)

    # Initialize if first request
    if raw_tokens is None:
        tokens = capacity
    else:
        tokens = float(raw_tokens)

    if raw_last_refill is None:
        last_refill = now
    else:
        last_refill = float(raw_last_refill)

    # Calculate refilled tokens based on elapsed time
    elapsed = now - last_refill
    if elapsed > 0:
        tokens = min(capacity, tokens + elapsed * refill_rate)

    # TTL for storage keys: enough time to cover a full refill cycle plus buffer
    ttl = int(capacity / refill_rate) + 1 if refill_rate > 0 else None

    # Attempt to consume one token
    if tokens >= 1:
        tokens -= 1
        # Update storage
        storage._set_raw(tokens_key, str(tokens), ttl)
        storage._set_raw(last_refill_key, str(now), ttl)

        remaining = int(tokens)
        # Time until bucket is fully refilled
        reset_after = (capacity - tokens) / refill_rate if refill_rate > 0 else 0.0

        return [1, remaining, int(capacity), reset_after, 0.0]
    else:
        # Denied — calculate how long until one token is available
        storage._set_raw(tokens_key, str(tokens), ttl)
        storage._set_raw(last_refill_key, str(now), ttl)

        # Time until at least 1 token is refilled
        deficit = 1.0 - tokens
        retry_after = deficit / refill_rate if refill_rate > 0 else 0.0
        reset_after = (capacity - tokens) / refill_rate if refill_rate > 0 else 0.0

        return [0, 0, int(capacity), reset_after, retry_after]


class TokenBucketAlgorithm(BaseAlgorithm):
    """Token Bucket rate limiting algorithm.

    The token bucket algorithm works by maintaining a bucket of tokens
    that refills at a steady rate. Each request consumes one token.
    If the bucket is empty, the request is denied.

    - Bucket capacity = rule.limit
    - Refill rate = rule.limit / rule.window tokens per second
    - Allows bursts up to the bucket capacity
    """

    def check(
        self, key: str, rule: RateLimitRule, storage: BaseStorage
    ) -> RateLimitResult:
        """Check if a request should be allowed under the token bucket algorithm.

        Args:
            key: Unique identifier for the rate limit subject.
            rule: The rate limiting rule to apply.
            storage: Storage backend to use for state.

        Returns:
            RateLimitResult indicating if the request is allowed
            and associated metadata.
        """
        capacity = rule.limit
        refill_rate = rule.limit / rule.window if rule.window > 0 else 0.0
        now = time.time()

        tokens_key = f"{key}:tokens"
        last_refill_key = f"{key}:last_refill"

        keys = [tokens_key, last_refill_key]
        args = [str(capacity), str(refill_rate), str(now)]

        # Detect storage type and use appropriate script
        if hasattr(storage, "_get_raw"):
            # In-memory backend: use callable
            result = storage.execute_atomic(
                _memory_token_bucket_script, keys, args
            )
        else:
            # Redis backend: use Lua script (to be implemented in Task 4.3)
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
