"""Clock policy shared by both atomic algorithm adapters."""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any

CLOCK_KEY_SUFFIX = ":clock-v2"


def clock_key(keys: list[str]) -> str:
    """Per-bucket high-watermark key, derived from the algorithm's first key."""
    return keys[0] + CLOCK_KEY_SUFFIX


def checked_time(value: Any) -> float:
    """Parse a stored/sampled clock, rejecting non-finite or absurd values."""
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1e12:
        raise ValueError("clock must be finite and in 0..1e12")
    return number


def delay_for_clock_regression(result: list[Any], skew: float) -> list[Any]:
    """Add a clamped clock regression to reset (and retry, when denied)."""
    if skew:
        result[3] = float(result[3]) + skew
        if not int(result[0]):
            result[4] = float(result[4]) + skew
    return result


@lru_cache(maxsize=16)
def with_server_clock(script: str) -> str:
    """Use Redis TIME inside the same EVAL, with a per-bucket high watermark.

    The adapter appends one declared clock key and a TTL argument. All five
    algorithms use ARGV[3] for time. A failed script does not publish a clock.
    Redis Lua errors still do not roll back earlier algorithm writes.
    """
    return (
        """
local clock_key = KEYS[#KEYS]
local sampled = redis.call('TIME')
local wall = tonumber(sampled[1]) + tonumber(sampled[2]) / 1000000
local previous = redis.call('GET', clock_key)
local now = wall
if previous ~= false then
    previous = tonumber(previous)
    if previous == nil or previous ~= previous or previous < 0 or previous > 1e12 then
        return redis.error_reply('invalid rate limiter clock')
    end
    now = math.max(now, previous)
end
ARGV[3] = tostring(now)
local result = (function()
"""
        + script
        + """
end)()
redis.call('SET', clock_key, tostring(now), 'EX', ARGV[#ARGV])
local skew = now - wall
if skew > 0 then
    result[4] = tostring(tonumber(result[4]) + skew)
    if tonumber(result[1]) == 0 then
        result[5] = tostring(tonumber(result[5]) + skew)
    end
end
return result
"""
    )
