"""Base algorithm interface and result types for rate limiting."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
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


class BaseAlgorithm(ABC):
    """Abstract base class for rate limiting algorithms.

    All rate limiting algorithms must implement the `check` method,
    which determines whether a request should be allowed based on
    the configured rule and current state in storage.
    """

    @abstractmethod
    def check(
        self, key: str, rule: "RateLimitRule", storage: "BaseStorage"
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
        ...
