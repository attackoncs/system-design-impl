"""Core RateLimiter orchestrator that ties algorithms, storage, and config together."""

from __future__ import annotations

import logging
from typing import Any, Optional

from .algorithms.base import BaseAlgorithm, RateLimitResult
from .algorithms.fixed_window import FixedWindowAlgorithm
from .algorithms.leaking_bucket import LeakingBucketAlgorithm
from .algorithms.sliding_window_counter import SlidingWindowCounterAlgorithm
from .algorithms.sliding_window_log import SlidingWindowLogAlgorithm
from .algorithms.token_bucket import TokenBucketAlgorithm
from .config import Algorithm, RateLimitRule, RateLimiterConfig
from .keys import ip_key
from .storage.base import BaseStorage
from .storage.memory import MemoryStorage
from .storage.redis import RedisStorage

logger = logging.getLogger(__name__)


class RateLimiter:
    """Main orchestrator that ties algorithms, storage, and config together.

    The RateLimiter class is the primary entry point for rate limiting operations.
    It initializes the appropriate storage backend and algorithm implementations,
    and provides methods to check rate limits against rules and requests.
    """

    def __init__(self, config: RateLimiterConfig) -> None:
        """Initialize the rate limiter with configuration.

        Args:
            config: RateLimiterConfig instance containing rules and settings.
        """
        self._config = config
        self._storage = self._create_storage()
        self._algorithms = self._load_algorithms()

    def _create_storage(self) -> BaseStorage:
        """Create the appropriate storage backend based on configuration.

        Returns:
            BaseStorage instance (MemoryStorage or RedisStorage).

        Raises:
            ValueError: If storage_backend is not recognized.
        """
        if self._config.storage_backend == "memory":
            return MemoryStorage()
        elif self._config.storage_backend == "redis":
            return RedisStorage(self._config.redis_url)
        else:
            raise ValueError(f"Unsupported storage backend: {self._config.storage_backend}")

    def _load_algorithms(self) -> dict[Algorithm, BaseAlgorithm]:
        """Load all algorithm implementations.

        Returns:
            Dictionary mapping Algorithm enum to algorithm instances.
        """
        return {
            Algorithm.TOKEN_BUCKET: TokenBucketAlgorithm(),
            Algorithm.LEAKING_BUCKET: LeakingBucketAlgorithm(),
            Algorithm.FIXED_WINDOW: FixedWindowAlgorithm(),
            Algorithm.SLIDING_WINDOW_LOG: SlidingWindowLogAlgorithm(),
            Algorithm.SLIDING_WINDOW_COUNTER: SlidingWindowCounterAlgorithm(),
        }

    def check(self, key: str, rule: RateLimitRule) -> RateLimitResult:
        """Check if a request should be allowed against a specific rule.

        Args:
            key: Unique identifier for the rate limit subject.
            rule: The rate limiting rule to apply.

        Returns:
            RateLimitResult indicating if the request is allowed and associated metadata.
        """
        algorithm = self._algorithms[rule.algorithm]
        try:
            return algorithm.check(key, rule, self._storage)
        except Exception as e:
            # Handle storage failures based on fail_open config
            if self._config.fail_open:
                logger.warning(
                    "Rate limiter storage error (fail-open, allowing request): %s", e
                )
                return RateLimitResult(
                    allowed=True,
                    remaining=rule.limit,
                    limit=rule.limit,
                    reset_after=0.0,
                    retry_after=None,
                )
            else:
                logger.warning(
                    "Rate limiter storage error (fail-closed, denying request): %s", e
                )
                return RateLimitResult(
                    allowed=False,
                    remaining=0,
                    limit=rule.limit,
                    reset_after=float(rule.window),
                    retry_after=float(rule.window),
                )

    def check_request(self, request_context: Any, rules: Optional[list[RateLimitRule]] = None) -> RateLimitResult:
        """Check a request against all applicable rules.

        Evaluates all rules against the request and returns the most restrictive
        result (first denial, or lowest remaining count if all allowed).

        Args:
            request_context: Request object or context containing information needed
                for key resolution (IP, user ID, etc.). Can also be a plain string
                to use directly as the rate limit key.
            rules: Optional list of rules to check. If None, uses all rules from config.

        Returns:
            RateLimitResult indicating if the request is allowed. If multiple rules
            apply, returns the most restrictive result (denial preferred over allowance,
            lowest remaining count if all allowed).
        """
        if rules is None:
            rules = self._config.rules

        if not rules:
            # No rules to check, allow by default
            return RateLimitResult(
                allowed=True,
                remaining=0,
                limit=0,
                reset_after=0.0,
                retry_after=None,
            )

        results = []
        for rule in rules:
            # Determine key for this rule
            key = self._resolve_key(request_context, rule)

            # Include rule parameters in the key to ensure different rules are isolated
            rule_suffix = f":{rule.window}:{rule.limit}:{rule.algorithm.value}"
            full_key = f"{self._config.key_prefix}{key}{rule_suffix}"

            result = self.check(full_key, rule)
            results.append(result)

        # Return the most restrictive result
        return self._most_restrictive_result(results)

    def _resolve_key(self, request_context: Any, rule: RateLimitRule) -> str:
        """Resolve the rate limit key from the request context and rule.

        Args:
            request_context: Request object or plain string identifier.
            rule: The rate limiting rule (may contain a custom key_func).

        Returns:
            The resolved key string.
        """
        # If request_context is a plain string, use it directly as the key
        if isinstance(request_context, str):
            return request_context

        # Use rule's custom key function if provided
        if rule.key_func:
            return rule.key_func(request_context)

        # Default to IP-based key resolution
        return ip_key(request_context)

    def _most_restrictive_result(self, results: list[RateLimitResult]) -> RateLimitResult:
        """Determine the most restrictive result from a list.

        Denial is preferred over allowance. If all allowed, the result with the
        lowest remaining count is returned.

        Args:
            results: List of RateLimitResult objects.

        Returns:
            The most restrictive RateLimitResult.
        """
        if not results:
            return RateLimitResult(
                allowed=True,
                remaining=0,
                limit=0,
                reset_after=0.0,
                retry_after=None,
            )

        # Find any denied results
        denied_results = [r for r in results if not r.allowed]
        if denied_results:
            # Return the denied result with the shortest retry_after
            return min(denied_results, key=lambda r: r.retry_after or float('inf'))

        # All allowed, return the result with the lowest remaining count
        return min(results, key=lambda r: r.remaining)

    def update_rules(self, rules: list[RateLimitRule]) -> None:
        """Dynamically update rules at runtime.

        Args:
            rules: New list of rate limiting rules to apply.
        """
        self._config.rules = rules

    @property
    def config(self) -> RateLimiterConfig:
        """Get the current configuration.

        Returns:
            The current RateLimiterConfig instance.
        """
        return self._config

    @property
    def storage(self) -> BaseStorage:
        """Get the storage backend instance.

        Returns:
            The BaseStorage instance being used.
        """
        return self._storage
