"""FastAPI middleware for rate limiting."""

from __future__ import annotations

from typing import Optional

import starlette.middleware.base
from starlette.concurrency import run_in_threadpool
import starlette.requests
import starlette.responses

from rate_limiter import config as rate_limiter_config
from rate_limiter import core as rate_limiter_core
from rate_limiter.middleware.configuration import RateLimitConfig


class RateLimitMiddleware(starlette.middleware.base.BaseHTTPMiddleware):
    """ASGI middleware for FastAPI rate limiting.

    This middleware provides rate limiting for FastAPI applications.
    It supports multiple rate limiting rules, custom key functions,
    and proper HTTP 429 responses with rate limit headers.

    Args:
        app: The FastAPI application.
        limiter: Optional RateLimiter instance. If not provided, a default
            in-memory limiter will be created.
        config: Optional rate_limiter_config.RateLimiterConfig. Used if limiter
            is not provided.
        auto_headers: If True, automatically adds rate limit headers to all
            responses.
    """

    def __init__(
        self,
        app,
        limiter: Optional[rate_limiter_core.RateLimiter] = None,
        config: Optional[rate_limiter_config.RateLimiterConfig] = None,
        auto_headers: bool = True,
    ) -> None:
        super().__init__(app)
        self.limiter = limiter or rate_limiter_core.RateLimiter(
            config or rate_limiter_config.RateLimiterConfig()
        )
        self.auto_headers = auto_headers

    async def dispatch(self, request: starlette.requests.Request, call_next):
        """Process the request through rate limiting middleware.

        Args:
            request: The incoming HTTP request.
            call_next: Function to call the next middleware/route handler.

        Returns:
            The response, either rate limited (429) or the actual response.
        """
        # Check rate limits
        # Storage backends are synchronous (Redis may wait on network I/O).
        # Use Starlette's bounded worker pool to keep the event loop responsive.
        result = await run_in_threadpool(self.limiter.check_request, request)

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

    def _rate_limit_response(self, result) -> starlette.responses.JSONResponse:
        """Create a 429 Too Many Requests response.

        Args:
            result: RateLimitResult from the check.

        Returns:
            JSONResponse with 429 status and error details.
        """
        return starlette.responses.JSONResponse(
            status_code=429,
            content={
                "error": "Too Many Requests",
                "message": (
                    "Rate limit exceeded. Try again in"
                    f" {result.retry_after or result.reset_after:.1f} seconds."
                ),
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
