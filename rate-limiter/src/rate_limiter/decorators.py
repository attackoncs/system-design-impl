"""Rate limiting decorator for function-level rate limiting."""

from __future__ import annotations

import asyncio
import functools
import inspect
import dataclasses
from typing import Any, Callable, Optional

from rate_limiter import config as rate_limiter_config
from rate_limiter import core as rate_limiter_core
from rate_limiter import keys as rate_limiter_keys
from rate_limiter.responses import rate_limit_headers, rejection_body


class RateLimitExceededException(Exception):
    """Exception raised when a rate limit is exceeded.

    This exception can be caught by applications to handle rate limiting in a
    custom way, such as returning a specific response format.
    """

    def __init__(
        self,
        limit: int,
        remaining: int,
        reset_after: float,
        retry_after: Optional[float] = None,
    ) -> None:
        self.limit = limit
        self.remaining = remaining
        self.reset_after = reset_after
        self.retry_after = retry_after
        super().__init__(
            f"Rate limit exceeded: {remaining} remaining out of {limit}, "
            f"retry after {retry_after or reset_after} seconds"
        )


def rate_limit(
    limit: int,
    window: int,
    algorithm: str = "token_bucket",
    key_func: Optional[Callable[[Any], str]] = None,
    limiter: Optional[rate_limiter_core.RateLimiter] = None,
    raise_exception: bool = True,
) -> Callable:
    """Decorator to apply rate limiting to a function or route handler.

    This decorator can be used to rate limit any function, including Flask and
    FastAPI route handlers. It supports custom key functions and can either
    raise an exception or return a response on rate limit denial.

    Args:
        limit: Maximum number of requests allowed in the time window.
        window: Time window in seconds.
        algorithm: Rate limiting algorithm to use (default: "token_bucket").
        key_func: Optional custom function to extract the rate limit key from
            the function arguments or request context.
        limiter: Optional RateLimiter instance. If not provided, a default
            in-memory limiter will be created.
        raise_exception: If True, raises RateLimitExceededException when rate
            limited. If False, returns a response with 429 status code.

    Returns:
        Decorated function that enforces rate limiting.

    Example:
        Basic usage:
        ```python
        @rate_limit(limit=100, window=60)
        def my_function():
            return "Hello, world!"
        ```

        With custom key function:
        ```python
        def user_key(request):
            return request.user.id

        @rate_limit(limit=10, window=60, key_func=user_key)
        def user_endpoint(request):
            return "User data"
        ```

        For Flask routes:
        ```python
        @app.route("/api/endpoint")
        @rate_limit(limit=50, window=60, raise_exception=False)
        def api_endpoint():
            return jsonify({"data": "value"})
        ```

        For FastAPI routes:
        ```python
        @app.get("/api/endpoint")
        @rate_limit(limit=50, window=60, raise_exception=False)
        async def api_endpoint():
            return {"data": "value"}
        ```
    """

    # Convert algorithm string to enum
    try:
        algo = rate_limiter_config.Algorithm(algorithm.lower())
    except ValueError as error:
        raise ValueError(
            f"Unknown algorithm: {algorithm}. Valid options:"
            f" {', '.join(a.value for a in rate_limiter_config.Algorithm)}"
        ) from error

    # Validate and construct once, including when a caller supplies the backend.
    rule = rate_limiter_config.RateLimitRule(
        limit=limit, window=window, algorithm=algo, key_func=key_func
    )
    if limiter is None:
        limiter = rate_limiter_core.RateLimiter(
            rate_limiter_config.RateLimiterConfig(rules=[rule])
        )

    def decorator(func: Callable) -> Callable:
        scoped_rule = dataclasses.replace(
            rule, name=f"{func.__module__}.{func.__qualname__}"[:256]
        )

        def resolve_key(args, kwargs):
            if key_func is not None:
                return key_func(*args, **kwargs)
            context = args[0] if args else kwargs.get("request")
            if context is not None:
                return rate_limiter_keys.ip_key(context)
            return f"func:{func.__name__}"

        def denied(result):
            if raise_exception:
                raise RateLimitExceededException(
                    limit=result.limit,
                    remaining=result.remaining,
                    reset_after=result.reset_after,
                    retry_after=result.retry_after,
                )
            return _rate_limit_response(result)

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                # Resolve request-bound keys on the event loop; only synchronous
                # storage work moves into the executor (with context propagation).
                key = resolve_key(args, kwargs)
                result = await asyncio.to_thread(limiter.check, key, scoped_rule)
                if not result.allowed:
                    return denied(result)
                return await func(*args, **kwargs)

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            result = limiter.check(resolve_key(args, kwargs), scoped_rule)
            if not result.allowed:
                return denied(result)
            return func(*args, **kwargs)

        return wrapper

    return decorator


def _rate_limit_response(result: Any) -> dict:
    """Create a rate limit response dictionary.

    Args:
        result: RateLimitResult from the check.

    Returns:
        Dictionary with error message and headers for 429 response.
    """
    return {
        "status_code": 429,
        "content": rejection_body(result),
        "headers": rate_limit_headers(result),
    }


# Convenience decorators for specific algorithms
def rate_limit_token_bucket(limit: int, window: int, **kwargs) -> Callable:
    """Rate limit using token bucket algorithm."""
    return rate_limit(limit=limit, window=window, algorithm="token_bucket", **kwargs)


def rate_limit_leaking_bucket(limit: int, window: int, **kwargs) -> Callable:
    """Rate limit using leaking bucket algorithm."""
    return rate_limit(limit=limit, window=window, algorithm="leaking_bucket", **kwargs)


def rate_limit_fixed_window(limit: int, window: int, **kwargs) -> Callable:
    """Rate limit using fixed window counter algorithm."""
    return rate_limit(limit=limit, window=window, algorithm="fixed_window", **kwargs)


def rate_limit_sliding_window(limit: int, window: int, **kwargs) -> Callable:
    """Rate limit using sliding window log algorithm."""
    return rate_limit(
        limit=limit, window=window, algorithm="sliding_window_log", **kwargs
    )


def rate_limit_sliding_counter(limit: int, window: int, **kwargs) -> Callable:
    """Rate limit using sliding window counter algorithm."""
    return rate_limit(
        limit=limit, window=window, algorithm="sliding_window_counter", **kwargs
    )
