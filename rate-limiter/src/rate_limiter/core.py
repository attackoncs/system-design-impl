"""Core RateLimiter orchestrator that ties algorithms, storage, and config
together.
"""

from __future__ import annotations

import logging
import dataclasses
import fnmatch
import hashlib
import json
import threading
from collections import Counter
from functools import lru_cache
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
from rate_limiter.rule_files import read_rules
from rate_limiter.storage import base as storage_base
from rate_limiter.storage import memory as storage_memory
from rate_limiter.storage import redis as storage_redis

logger = logging.getLogger(__name__)

_ALGORITHM_TYPES: dict[rate_limiter_config.Algorithm, type] = {
    rate_limiter_config.Algorithm.TOKEN_BUCKET: token_bucket.TokenBucketAlgorithm,
    rate_limiter_config.Algorithm.LEAKING_BUCKET: (
        leaking_bucket.LeakingBucketAlgorithm
    ),
    rate_limiter_config.Algorithm.FIXED_WINDOW: fixed_window.FixedWindowAlgorithm,
    rate_limiter_config.Algorithm.SLIDING_WINDOW_LOG: (
        sliding_window_log.SlidingWindowLogAlgorithm
    ),
    rate_limiter_config.Algorithm.SLIDING_WINDOW_COUNTER: (
        sliding_window_counter.SlidingWindowCounterAlgorithm
    ),
}

_MAX_KEY_LENGTH = 8192


def _checked_key(key: Any, message: str) -> str:
    """Bound subject length before any rule charges storage."""
    if not isinstance(key, str) or not 1 <= len(key) <= _MAX_KEY_LENGTH:
        raise ValueError(message)
    return key


def _callable_name(func: Any) -> Optional[str]:
    """Stable module-qualified name of a key resolver (part of policy identity)."""
    if func is None:
        return None
    module = getattr(func, "__module__", type(func).__module__)
    qualname = getattr(func, "__qualname__", type(func).__qualname__)
    return f"{module}.{qualname}"


@lru_cache(maxsize=512)
def _scope_digest(identity):
    """Cache bounded policy metadata, never per-subject data."""
    return hashlib.sha256(
        json.dumps(identity, separators=(",", ":")).encode()
    ).hexdigest()


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
        self._lock = threading.RLock()
        self._metrics = Counter()
        self._config = dataclasses.replace(config, rules=self._copy_rules(config.rules))
        self._storage = storage if storage is not None else self._create_storage()
        self._algorithms = self._load_algorithms()

    @staticmethod
    def _copy_rules(rules):
        if not isinstance(rules, (list, tuple)) or len(rules) > 100:
            raise ValueError("at most 100 rules are supported")
        copied = []
        for rule in rules:
            if not isinstance(rule, rate_limiter_config.RateLimitRule):
                raise ValueError("expected RateLimitRule")
            copied.append(dataclasses.replace(rule))
        return copied

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
        """Create one stateless instance per supported algorithm."""
        return {algorithm: cls() for algorithm, cls in _ALGORITHM_TYPES.items()}

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
        rule = dataclasses.replace(rule)
        _checked_key(key, "key must have 1..8192 characters")
        return self._evaluate(self._state_key(key, rule), rule)

    def _state_key(self, key, rule):
        identity = (
            rule.name,
            rule.path_pattern,
            rule.key_type,
            _callable_name(rule.key_func),
            rule.capacity,
            rule.window,
            rule.limit,
            rule.algorithm.value,
        )
        scope = _scope_digest(identity)
        subject = hashlib.sha256(key.encode()).hexdigest()
        return f"{self._config.key_prefix}v2:{{{subject}}}:{scope}"

    def _evaluate(self, key, rule):
        algorithm = self._algorithms[rule.algorithm]
        failed = False
        try:
            result = algorithm.check(key, rule, self._storage)
            if result.limit != rule.effective_limit:
                raise ValueError("storage returned the wrong limit")
        # Isolate this operation; record failures before continuing.
        except Exception as e:  # pylint: disable=broad-exception-caught
            failed = True
            logger.warning(
                "Rate limiter storage failure (%s, %s)",
                "fail-open" if self._config.fail_open else "fail-closed",
                type(e).__name__,
            )
            result = self._failure_result(rule)
        with self._lock:
            self._metrics["checks"] += 1
            self._metrics["allowed" if result.allowed else "denied"] += 1
            self._metrics["storage_errors"] += int(failed)
        return result

    def _failure_result(self, rule) -> algorithms_base.RateLimitResult:
        """Apply the configured fail-open/fail-closed policy to one rule."""
        limit = rule.effective_limit
        if self._config.fail_open:
            return algorithms_base.RateLimitResult(
                allowed=True,
                remaining=limit,
                limit=limit,
                reset_after=0.0,
                retry_after=None,
            )
        return algorithms_base.RateLimitResult(
            allowed=False,
            remaining=0,
            limit=limit,
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
            with self._lock:
                rules = tuple(self._config.rules)
        else:
            rules = self._copy_rules(rules)

        results = []
        seen = set()
        for rule in rules:
            if not self._rule_applies(request_context, rule):
                continue
            # The state key includes policy identity, so different rules are
            # isolated and identical rules are charged once per request.
            full_key = self._state_key(self._resolve_key(request_context, rule), rule)
            if full_key in seen:
                continue
            seen.add(full_key)
            results.append(self._evaluate(full_key, rule))

        # No applicable rules allows by default.
        return self._most_restrictive_result(results)

    @staticmethod
    def _rule_applies(
        request_context: Any, rule: rate_limiter_config.RateLimitRule
    ) -> bool:
        """Path-filtered rules match only request objects with a matching path."""
        if rule.path_pattern is None:
            return True
        return not isinstance(request_context, str) and fnmatch.fnmatchcase(
            rate_limiter_keys.path_key(request_context), rule.path_pattern
        )

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
            return _checked_key(request_context, "key must have 1..8192 characters")

        # Selectors are resolved before storage, outside the config lock.
        resolver = rule.key_func
        if resolver is None:
            resolver = rate_limiter_keys.KEY_TYPE_FUNCTIONS[rule.key_type]
        return _checked_key(
            resolver(request_context), "resolved key must have 1..8192 characters"
        )

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
                allowed=True, remaining=0, limit=0, reset_after=0.0, retry_after=None
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
        copied = self._copy_rules(rules)
        with self._lock:
            self._config = dataclasses.replace(self._config, rules=copied)

    def reload_rules(self, path) -> int:
        """Validate a complete JSON rule file, then atomically publish it."""
        rules = read_rules(path)
        self.update_rules(rules)
        return len(rules)

    def metrics(self) -> dict[str, int]:
        """Bounded counters per rule evaluation, without subject labels."""
        with self._lock:
            return {
                name: self._metrics[name]
                for name in ("checks", "allowed", "denied", "storage_errors")
            }

    @property
    def config(self) -> rate_limiter_config.RateLimiterConfig:
        """Get the current configuration.

        Returns:
            The current RateLimiterConfig instance.
        """
        with self._lock:
            return dataclasses.replace(
                self._config, rules=self._copy_rules(self._config.rules)
            )

    @property
    def storage(self) -> storage_base.BaseStorage:
        """Get the storage backend instance.

        Returns:
            The BaseStorage instance being used.
        """
        return self._storage
