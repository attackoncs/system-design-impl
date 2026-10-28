"""Shared wire representation for framework adapters and decorators."""

from __future__ import annotations

import math


def rate_limit_headers(result) -> dict[str, str]:
    """Expose relative seconds; HTTP Retry-After uses whole delta-seconds."""
    headers = {
        "X-RateLimit-Limit": str(result.limit),
        "X-RateLimit-Remaining": str(result.remaining),
        "X-RateLimit-Reset": str(math.ceil(result.reset_after)),
    }
    if not result.allowed:
        delay = (
            result.retry_after if result.retry_after is not None else result.reset_after
        )
        headers["Retry-After"] = str(max(1, math.ceil(delay)))
        headers["X-RateLimit-Retry-After"] = headers["Retry-After"]
    return headers


def rejection_body(result) -> dict:
    delay = result.retry_after if result.retry_after is not None else result.reset_after
    return {
        "error": "Too Many Requests",
        "message": f"Rate limit exceeded. Try again in {delay:.1f} seconds.",
        "retry_after": delay,
    }
