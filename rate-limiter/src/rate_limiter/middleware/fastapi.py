"""FastAPI middleware for rate limiting."""

from __future__ import annotations

from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..config import RateLimitRule, RateLimiterConfig
from ..core import RateLimiter
from ..keys import ip_key, user_id_key, path_key, method_key


class RateLimitMiddleware(BaseHTTPMiddleware):
    """ASGI middleware for FastAPI rate limiting.

    This middleware provides rate limiting for FastAPI applications.
    It supports multiple rate limiting rules, custom key functions,
    and proper HTTP 429 responses with rate limit headers.

    Args:
        app: The FastAPI application.
        limiter: Optional RateLimiter instance. If not provided, a default
            in-memory limiter will be created.
        config: Optional RateLimiterConfig. Used if limiter is not provided.
        auto_headers: If True, automatically adds rate limit headers to all responses.
    """

    def __init__(
        self,
        app,
        limiter: Optional[RateLimiter] = None,
        config: Optional[RateLimiterConfig] = None,
        auto_headers: bool = True,
    ) -> None:
        super().__init__(app)
        self.limiter = limiter or RateLimiter(config or RateLimiterConfig())
        self.auto_headers = auto_headers

    async def dispatch(self, request: Request, call_next):
        """Process the request through rate limiting middleware.

        Args:
            request: The incoming HTTP request.
            call_next: Function to call the next middleware/route handler.

        Returns:
            The response, either rate limited (429) or the actual response.
        """
        # Check rate limits
        result = self.limiter.check_request(request)

        # If rate limited, return 429 response
        if not result.allowed:
            response = self._rate_limit_response(result)
            return response

        # Process the request
        response = await call_next(request)

        # Add rate limit headers if enabled
        if self.auto_headers:
            self._add_rate_limit_headers(response, result)

        return response

    def _rate_limit_response(self, result) -> JSONResponse:
        """Create a 429 Too Many Requests response.

        Args:
            result: RateLimitResult from the check.

        Returns:
            JSONResponse with 429 status and error details.
        """
        return JSONResponse(
            status_code=429,
            content={
                "error": "Too Many Requests",
                "message": f"Rate limit exceeded. Try again in {result.retry_after or result.reset_after:.1f} seconds.",
                "retry_after": result.retry_after or result.reset_after,
            },
            headers={
                "X-RateLimit-Limit": str(result.limit),
                "X-RateLimit-Remaining": str(result.remaining),
                "X-RateLimit-Reset": str(result.reset_after),
                "Retry-After": str(result.retry_after or result.reset_after),
            },
        )

    def _add_rate_limit_headers(self, response, result):
        """Add rate limit headers to a response.

        Args:
            response: The response to add headers to.
            result: RateLimitResult containing the rate limit information.
        """
        response.headers["X-RateLimit-Limit"] = str(result.limit)
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)
        response.headers["X-RateLimit-Reset"] = str(result.reset_after)


class RateLimitConfig:
    """Configuration helper for rate limiting.

    Provides a convenient way to configure rate limiting for FastAPI.

    Example:
        ```python
        from fastapi import FastAPI
        from rate_limiter.middleware.fastapi import RateLimitConfig

        app = FastAPI()

        # Configure rate limiting
        rate_limit_config = RateLimitConfig()
        rate_limit_config.add_rule(
            limit=100, window=60, key_type="ip"
        )
        rate_limit_config.add_rule(
            limit=1000, window=3600, key_type="user_id"
        )

        # Add middleware
        app.add_middleware(RateLimitMiddleware, config=rate_limit_config.get_config())
        ```
    """

    def __init__(self) -> None:
        self.rules: list[RateLimitRule] = []

    def add_rule(
        self,
        limit: int,
        window: int,
        algorithm: str = "token_bucket",
        key_type: str = "ip",
        path_pattern: Optional[str] = None,
    ) -> None:
        """Add a rate limiting rule.

        Args:
            limit: Maximum number of requests allowed in the window.
            window: Time window in seconds.
            algorithm: Rate limiting algorithm to use.
            key_type: Type of key to use ('ip', 'user_id', 'path', 'method').
            path_pattern: Optional path pattern to match (not implemented yet).
        """
        from ..config import Algorithm

        try:
            algo = Algorithm(algorithm.lower())
        except ValueError:
            raise ValueError(
                f"Unknown algorithm: {algorithm}. "
                f"Valid options: {', '.join(a.value for a in Algorithm)}"
            )

        # Create key function based on type
        if key_type == "ip":
            key_func = ip_key
        elif key_type == "user_id":
            key_func = user_id_key
        elif key_type == "path":
            key_func = path_key
        elif key_type == "method":
            key_func = method_key
        else:
            raise ValueError(f"Unknown key type: {key_type}")

        rule = RateLimitRule(
            limit=limit,
            window=window,
            algorithm=algo,
            key_func=key_func,
            path_pattern=path_pattern,
        )
        self.rules.append(rule)

    def get_config(self) -> RateLimiterConfig:
        """Get the RateLimiterConfig for use with middleware.

        Returns:
            Configured RateLimiterConfig instance.
        """
        return RateLimiterConfig(rules=self.rules)


# Convenience function to create a rate limited FastAPI app
def create_rate_limited_app(
    app,
    limit: int = 100,
    window: int = 60,
    algorithm: str = "token_bucket",
    key_type: str = "ip",
) -> RateLimitMiddleware:
    """Create a rate limited FastAPI application.

    Args:
        app: The FastAPI application.
        limit: Maximum number of requests per window (default: 100).
        window: Time window in seconds (default: 60).
        algorithm: Rate limiting algorithm (default: "token_bucket").
        key_type: Key type for rate limiting (default: "ip").

    Returns:
        RateLimitMiddleware instance.

    Example:
        ```python
        from fastapi import FastAPI
        from rate_limiter.middleware.fastapi import create_rate_limited_app

        app = FastAPI()

        # Add rate limiting
        app.add_middleware(
            create_rate_limited_app,
            limit=100,
            window=60,
            key_type="ip"
        )
        ```
    """
    config = RateLimitConfig()
    config.add_rule(limit=limit, window=window, algorithm=algorithm, key_type=key_type)
    return RateLimitMiddleware(app, config=config.get_config())