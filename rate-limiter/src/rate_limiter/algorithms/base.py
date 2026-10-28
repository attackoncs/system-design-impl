"""Base algorithm interface and result types for rate limiting."""

from __future__ import annotations

import abc
import dataclasses
import math

from rate_limiter import config
from rate_limiter.storage import base


@dataclasses.dataclass
class RateLimitResult:
    """Result of a rate limit check.

    Attributes:
        allowed: Whether the request is allowed.
        remaining: Remaining requests in the current window.
        limit: The maximum number of requests allowed.
        reset_after: Seconds until the limit resets.
        retry_after: Seconds to wait before retrying, or None if allowed.
    """

    allowed: bool
    remaining: int
    limit: int
    reset_after: float
    retry_after: float | None

    @classmethod
    def from_storage(cls, values) -> "RateLimitResult":
        """Decode the five-field result shared by Lua and memory callbacks."""
        if not isinstance(values, (list, tuple)) or len(values) != 5:
            raise ValueError("storage result must have five fields")
        integers = []
        for value in values[:3]:
            if isinstance(value, bool):
                raise ValueError("storage integers must not be bool")
            if type(value) is int:
                number = value
            else:
                number = int(value)
                if float(value) != number:
                    raise ValueError("storage result contains a fractional integer")
            integers.append(number)
        flag, remaining, limit = integers
        reset, retry = float(values[3]), float(values[4])
        if flag not in (0, 1) or not 0 <= remaining <= limit or limit <= 0:
            raise ValueError("invalid storage counters")
        if not all(math.isfinite(value) and value >= 0 for value in (reset, retry)):
            raise ValueError("invalid storage delay")
        if (not flag and (remaining != 0 or retry <= 0)) or (flag and retry != 0):
            raise ValueError("inconsistent storage decision")
        allowed = bool(flag)
        return cls(
            allowed=allowed,
            remaining=remaining,
            limit=limit,
            reset_after=reset,
            retry_after=None if allowed else retry,
        )


class BaseAlgorithm(abc.ABC):
    """Abstract base class for rate limiting algorithms.

    All rate limiting algorithms must implement the `check` method, which
    determines whether a request should be allowed based on the configured rule
    and current state in storage.
    """

    @abc.abstractmethod
    def check(
        self, key: str, rule: config.RateLimitRule, storage: base.BaseStorage
    ) -> RateLimitResult:
        """Check if a request should be allowed.

        Args:
            key: Unique identifier for the rate limit subject.
            rule: The rate limiting rule to apply.
            storage: Storage backend to use for state.

        Returns:
            RateLimitResult indicating if the request is allowed
            and associated metadata.
        """
