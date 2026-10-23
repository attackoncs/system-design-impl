"""Base algorithm interface and result types for rate limiting."""

from __future__ import annotations

import abc
import dataclasses

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
        allowed = bool(int(values[0]))
        return cls(
            allowed=allowed,
            remaining=int(values[1]),
            limit=int(values[2]),
            reset_after=float(values[3]),
            retry_after=None if allowed else float(values[4]),
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
