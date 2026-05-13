"""Rate limiting decorator for function-level rate limiting."""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable, Optional

from .config import RateLimitRule
from .core import RateLimiter


class RateLimitExceededException(Exception):
    """Exception raised when a rate limit is exceeded.

    This exception can be caught by applications to handle rate limiting
    in a custom way, such as returning a specific response format.
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
    limiter: Optional[RateLimiter] = None,
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
    from .config import Algorithm

    # Convert algorithm string to enum
    try:
        algo = Algorithm(algorithm.lower())
    except ValueError:
        raise ValueError(
            f"Unknown algorithm: {algorithm}. "
            f"Valid options: {', '.join(a.value for a in Algorithm)}"
        )

    # Create a limiter if not provided
    if limiter is None:
        from .config import RateLimiterConfig

        rule = RateLimitRule(limit=limit, window=window, algorithm=algo)
        config = RateLimiterConfig(rules=[rule])
        limiter = RateLimiter(config)

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            # Determine the key for rate limiting
            if key_func is not None:
                # Use custom key function
                key = key_func(*args, **kwargs)
            else:
                # Try to extract from request context
                # For Flask/FastAPI, the first argument is often the request
                request_context = args[0] if args else kwargs.get('request', None)
                if request_context is not None:
                    from .keys import ip_key

                    key = ip_key(request_context)
                else:
                    # Fallback to function name
                    key = f"func:{func.__name__}"

            # Check rate limit using the limiter's config
            rule = RateLimitRule(limit=limit, window=window, algorithm=algo, key_func=key_func)
            result = limiter.check(key, rule)

            if not result.allowed:
                if raise_exception:
                    raise RateLimitExceededException(
                        limit=result.limit,
                        remaining=result.remaining,
                        reset_after=result.reset_after,
                        retry_after=result.retry_after,
                    )
                else:
                    # Return a 429 response (for web frameworks)
                    return _rate_limit_response(result)

            # Call the original function
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
        "content": {
            "error": "Too Many Requests",
            "message": f"Rate limit exceeded. Try again in {result.retry_after or result.reset_after:.1f} seconds.",
            "retry_after": result.retry_after or result.reset_after,
        },
        "headers": {
            "X-RateLimit-Limit": str(result.limit),
            "X-RateLimit-Remaining": str(result.remaining),
            "X-RateLimit-Reset": str(result.reset_after),
            "Retry-After": str(result.retry_after or result.reset_after),
        },
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
    return rate_limit(limit=limit, window=window, algorithm="sliding_window_log", **kwargs)


def rate_limit_sliding_counter(limit: int, window: int, **kwargs) -> Callable:
    """Rate limit using sliding window counter algorithm."""
    return rate_limit(limit=limit, window=window, algorithm="sliding_window_counter", **kwargs)