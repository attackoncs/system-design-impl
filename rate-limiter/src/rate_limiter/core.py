"""Core RateLimiter orchestrator that ties algorithms, storage, and config
together.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from rate_limiter import config as rate_limiter_config
from rate_limiter import keys as rate_limiter_keys
from rate_limiter.algorithms import base as algorithms_base
from rate_limiter.algorithms import (
    fixed_window,
    leaking_bucket,
    sliding_window_counter,
    sliding_window_log,
    token_bucket,
)
from rate_limiter.storage import base as storage_base
from rate_limiter.storage import memory as storage_memory
from rate_limiter.storage import redis as storage_redis

logger = logging.getLogger(__name__)


class RateLimiter:
    """Main orchestrator that ties algorithms, storage, and config together.

    The RateLimiter class is the primary entry point for rate limiting
    operations. It initializes the appropriate storage backend and algorithm
    implementations, and provides methods to check rate limits against rules
    and requests.
    """

    def __init__(
        self,
        config: rate_limiter_config.RateLimiterConfig,
        *,
        storage: Optional[storage_base.BaseStorage] = None,
    ) -> None:
        """Initialize the rate limiter with configuration.

        Args:
            config: RateLimiterConfig instance containing rules and settings.
            storage: Optional shared or custom backend. When omitted, create
                the backend configured by config.
        """
        self._config = config
        self._storage = storage if storage is not None else self._create_storage()
        self._algorithms = self._load_algorithms()

    def _create_storage(self) -> storage_base.BaseStorage:
        """Create the appropriate storage backend based on configuration.

        Returns:
            BaseStorage instance (MemoryStorage or RedisStorage).

        Raises:
            ValueError: If storage_backend is not recognized.
        """
        if self._config.storage_backend == "memory":
            return storage_memory.MemoryStorage()
        elif self._config.storage_backend == "redis":
            return storage_redis.RedisStorage(self._config.redis_url)
        else:
            raise ValueError(
                f"Unsupported storage backend: {self._config.storage_backend}"
            )

    def _load_algorithms(
        self,
    ) -> dict[
        rate_limiter_config.Algorithm,
        algorithms_base.BaseAlgorithm,
    ]:
        """Load all algorithm implementations.

        Returns:
            Dictionary mapping Algorithm enum to algorithm instances.
        """
        return {
            rate_limiter_config.Algorithm.TOKEN_BUCKET: (
                token_bucket.TokenBucketAlgorithm()
            ),
            rate_limiter_config.Algorithm.LEAKING_BUCKET: (
                leaking_bucket.LeakingBucketAlgorithm()
            ),
            rate_limiter_config.Algorithm.FIXED_WINDOW: (
                fixed_window.FixedWindowAlgorithm()
            ),
            rate_limiter_config.Algorithm.SLIDING_WINDOW_LOG: (
                sliding_window_log.SlidingWindowLogAlgorithm()
            ),
            rate_limiter_config.Algorithm.SLIDING_WINDOW_COUNTER: (
                sliding_window_counter.SlidingWindowCounterAlgorithm()
            ),
        }

    def check(
        self, key: str, rule: rate_limiter_config.RateLimitRule
    ) -> algorithms_base.RateLimitResult:
        """Check if a request should be allowed against a specific rule.

        Args:
            key: Unique identifier for the rate limit subject.
            rule: The rate limiting rule to apply.

        Returns:
            RateLimitResult indicating if the request is allowed and associated
            metadata.
        """
        algorithm = self._algorithms[rule.algorithm]
        try:
            return algorithm.check(key, rule, self._storage)
        # Isolate this operation; record failures before continuing.
        except Exception as e:  # pylint: disable=broad-exception-caught
            # Handle storage failures based on fail_open config
            logging.getLogger(__name__).exception("Failure isolated in %s", "check")
            if self._config.fail_open:
                logger.warning(
                    "Rate limiter storage error (fail-open, allowing request): %s",
                    e,
                )
                return algorithms_base.RateLimitResult(
                    allowed=True,
                    remaining=rule.limit,
                    limit=rule.limit,
                    reset_after=0.0,
                    retry_after=None,
                )
            else:
                logger.warning(
                    "Rate limiter storage error (fail-closed, denying request): %s",
                    e,
                )
                return algorithms_base.RateLimitResult(
                    allowed=False,
                    remaining=0,
                    limit=rule.limit,
                    reset_after=float(rule.window),
                    retry_after=float(rule.window),
                )

    def check_request(
        self,
        request_context: Any,
        rules: Optional[list[rate_limiter_config.RateLimitRule]] = None,
    ) -> algorithms_base.RateLimitResult:
        """Check a request against all applicable rules.

        Evaluates all rules against the request and returns the most restrictive
        result (longest retry delay, or lowest remaining count if all allowed).

        Args:
            request_context: Request object or context containing information
                needed
                for key resolution (IP, user ID, etc.). Can also be a plain
                string
                to use directly as the rate limit key.
            rules: Rules to check; None uses all configured rules.

        Returns:
            RateLimitResult indicating if the request is allowed. If multiple
            rules
            apply, returns the most restrictive result (denial preferred over
            allowance,
            lowest remaining count if all allowed).
        """
        if rules is None:
            rules = self._config.rules

        if not rules:
            # No rules to check, allow by default
            return algorithms_base.RateLimitResult(
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

            # Include rule parameters in the key to ensure different rules are
            # isolated
            rule_suffix = f":{rule.window}:{rule.limit}:{rule.algorithm.value}"
            full_key = f"{self._config.key_prefix}{key}{rule_suffix}"

            result = self.check(full_key, rule)
            results.append(result)

        # Return the most restrictive result
        return self._most_restrictive_result(results)

    def _resolve_key(
        self, request_context: Any, rule: rate_limiter_config.RateLimitRule
    ) -> str:
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
        return rate_limiter_keys.ip_key(request_context)

    def _most_restrictive_result(
        self, results: list[algorithms_base.RateLimitResult]
    ) -> algorithms_base.RateLimitResult:
        """Determine the most restrictive result from a list.

        Denial is preferred over allowance. If all allowed, the result with the
        lowest remaining count is returned.

        Args:
            results: List of RateLimitResult objects.

        Returns:
            The most restrictive RateLimitResult.
        """
        if not results:
            return algorithms_base.RateLimitResult(
                allowed=True,
                remaining=0,
                limit=0,
                reset_after=0.0,
                retry_after=None,
            )

        # Find any denied results
        denied_results = [r for r in results if not r.allowed]
        if denied_results:
            # Every rule must allow the retry, so use the longest delay.
            return max(
                denied_results,
                key=lambda r: (
                    r.retry_after if r.retry_after is not None else r.reset_after
                ),
            )

        # All allowed, return the result with the lowest remaining count
        return min(results, key=lambda r: r.remaining)

    def update_rules(self, rules: list[rate_limiter_config.RateLimitRule]) -> None:
        """Dynamically update rules at runtime.

        Args:
            rules: New list of rate limiting rules to apply.
        """
        self._config.rules = rules

    @property
    def config(self) -> rate_limiter_config.RateLimiterConfig:
        """Get the current configuration.

        Returns:
            The current RateLimiterConfig instance.
        """
        return self._config

    @property
    def storage(self) -> storage_base.BaseStorage:
        """Get the storage backend instance.

        Returns:
            The BaseStorage instance being used.
        """
        return self._storage
