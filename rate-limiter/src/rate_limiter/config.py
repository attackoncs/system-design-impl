"""Configuration models for the rate limiter."""

from __future__ import annotations

import dataclasses
import enum
from typing import Callable, Optional


class Algorithm(enum.Enum):
    """Supported rate limiting algorithms."""

    TOKEN_BUCKET = "token_bucket"
    LEAKING_BUCKET = "leaking_bucket"
    FIXED_WINDOW = "fixed_window"
    SLIDING_WINDOW_LOG = "sliding_window_log"
    SLIDING_WINDOW_COUNTER = "sliding_window_counter"


@dataclasses.dataclass
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
    capacity: Optional[int] = None

    def __post_init__(self) -> None:
        for name in ("limit", "window"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 1_000_000_000:
                raise ValueError(f"{name} must be an integer in 1..1000000000")
        if not isinstance(self.algorithm, Algorithm):
            raise ValueError("algorithm must be an Algorithm enum member")
        if self.key_func is not None and not callable(self.key_func):
            raise ValueError("key_func must be callable")
        if self.key_type not in {"ip", "user_id", "path", "method", "custom"}:
            raise ValueError("unknown key_type")
        if self.key_type == "custom" and self.key_func is None:
            raise ValueError("custom key_type requires key_func")
        for name in ("name", "path_pattern"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not 1 <= len(value) <= 256
            ):
                raise ValueError(
                    f"{name} must be a nonempty string of at most 256 characters"
                )
        if self.path_pattern is not None and not self.path_pattern.startswith("/"):
            raise ValueError("path_pattern must start with /")
        if self.capacity is not None:
            if (
                type(self.capacity) is not int
                or not 1 <= self.capacity <= 1_000_000_000
            ):
                raise ValueError("capacity must be an integer in 1..1000000000")
            if self.algorithm not in {Algorithm.TOKEN_BUCKET, Algorithm.LEAKING_BUCKET}:
                raise ValueError("capacity applies only to token and leaking buckets")
            if self.capacity * self.window > 1_000_000_000 * self.limit:
                raise ValueError("bucket drain time must not exceed 1000000000 seconds")


@dataclasses.dataclass
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

    rules: list[RateLimitRule] = dataclasses.field(default_factory=list)
    storage_backend: str = "memory"
    redis_url: str = "redis://localhost:6379"
    key_prefix: str = "rl:"
    default_response_code: int = 429
    include_headers: bool = True
    fail_open: bool = True

    def __post_init__(self) -> None:
        if self.storage_backend not in {"memory", "redis"}:
            raise ValueError(f"Unsupported storage backend: {self.storage_backend}")
        if not isinstance(self.key_prefix, str) or not 1 <= len(self.key_prefix) <= 128:
            raise ValueError("key_prefix must have 1..128 characters")
        if (
            type(self.default_response_code) is not int
            or not 400 <= self.default_response_code <= 599
        ):
            raise ValueError("default_response_code must be in 400..599")
        if type(self.include_headers) is not bool or type(self.fail_open) is not bool:
            raise ValueError("include_headers and fail_open must be bool")
        if not isinstance(self.rules, (list, tuple)) or len(self.rules) > 100:
            raise ValueError("rules must contain at most 100 entries")
        if any(not isinstance(rule, RateLimitRule) for rule in self.rules):
            raise ValueError("rules must contain RateLimitRule instances")
