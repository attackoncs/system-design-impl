"""Configuration models for the rate limiter."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class Algorithm(Enum):
    """Supported rate limiting algorithms."""

    TOKEN_BUCKET = "token_bucket"
    LEAKING_BUCKET = "leaking_bucket"
    FIXED_WINDOW = "fixed_window"
    SLIDING_WINDOW_LOG = "sliding_window_log"
    SLIDING_WINDOW_COUNTER = "sliding_window_counter"


@dataclass
class RateLimitRule:
    """A single rate limiting rule.

    Attributes:
        limit: Maximum number of requests allowed in the window.
        window: Time window in seconds.
        algorithm: Which rate limiting algorithm to use.
        key_func: Optional custom key resolver function.
        key_type: Type of key to use ("ip", "user_id", "custom").
        path_pattern: Optional URL path pattern to match (for middleware).
        name: Optional human-readable rule name for identification.
    """

    limit: int
    window: int
    algorithm: Algorithm = Algorithm.TOKEN_BUCKET
    key_func: Optional[Callable] = None
    key_type: str = "ip"
    path_pattern: Optional[str] = None
    name: Optional[str] = None


@dataclass
class RateLimiterConfig:
    """Top-level rate limiter configuration.

    Attributes:
        rules: List of rate limit rules to apply.
        storage_backend: Storage type - "memory" or "redis".
        redis_url: Redis connection URL for distributed deployments.
        key_prefix: Prefix for storage keys to avoid collisions.
        default_response_code: HTTP status code for rate limited responses.
        include_headers: Whether to include rate limit headers in responses.
        fail_open: If True, allow requests when storage is unavailable.
            If False, deny requests when storage is unavailable.
    """

    rules: list[RateLimitRule] = field(default_factory=list)
    storage_backend: str = "memory"
    redis_url: str = "redis://localhost:6379"
    key_prefix: str = "rl:"
    default_response_code: int = 429
    include_headers: bool = True
    fail_open: bool = True
