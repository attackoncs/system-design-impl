"""Flask middleware/extension for rate limiting."""

from __future__ import annotations

from typing import Optional

from flask import Flask, request, jsonify, g

from ..config import RateLimitRule, RateLimiterConfig
from ..core import RateLimiter
from ..keys import ip_key, user_id_key, path_key, method_key


class RateLimitExtension:
    """Flask extension for rate limiting.

    This extension provides rate limiting for Flask applications.
    It registers before_request and after_request hooks to enforce
    rate limits and add rate limit headers to responses.

    Args:
        app: Optional Flask application instance.
        limiter: Optional RateLimiter instance. If not provided, a default
            in-memory limiter will be created.
        config: Optional RateLimiterConfig. Used if limiter is not provided.
        auto_headers: If True, automatically adds rate limit headers to all responses.

    Example:
        ```python
        from flask import Flask
        from rate_limiter.middleware.flask import RateLimitExtension

        app = Flask(__name__)

        # Initialize rate limiting
        rate_limiter = RateLimitExtension(app)

        # Or with custom config
        from rate_limiter.config import RateLimiterConfig, RateLimitRule
        from rate_limiter.core import RateLimiter

        rules = [
            RateLimitRule(limit=100, window=60),  # 100 requests per minute
            RateLimitRule(limit=1000, window=3600),  # 1000 requests per hour
        ]
        config = RateLimiterConfig(rules=rules)
        limiter = RateLimiter(config)
        rate_limiter = RateLimitExtension(app, limiter=limiter)
        ```
    """

    def __init__(
        self,
        app: Optional[Flask] = None,
        limiter: Optional[RateLimiter] = None,
        config: Optional[RateLimiterConfig] = None,
        auto_headers: bool = True,
    ) -> None:
        self.limiter = limiter or RateLimiter(config or RateLimiterConfig())
        self.auto_headers = auto_headers

        if app is not None:
            self.init_app(app)

    def init_app(self, app: Flask) -> None:
        """Initialize the extension with a Flask application.

        Args:
            app: Flask application instance.
        """
        # Register before_request hook to check rate limits
        app.before_request(self._check_rate_limit)

        # Register after_request hook to add headers
        if self.auto_headers:
            app.after_request(self._add_rate_limit_headers)

        # Store the extension instance on the app
        app.extensions["rate_limiter"] = self

    def _check_rate_limit(self) -> Optional[dict]:
        """Check rate limits before processing a request.

        This method is called by Flask's before_request hook.

        Returns:
            Optional error response dict if rate limited, None otherwise.
        """
        # Check rate limits
        result = self.limiter.check_request(request)

        # If rate limited, store the result and return error response
        if not result.allowed:
            g.rate_limit_result = result
            return self._rate_limit_response(result)

        # Store the result for header injection
        g.rate_limit_result = result
        return None

    def _rate_limit_response(self, result) -> dict:
        """Create a 429 Too Many Requests response.

        Args:
            result: RateLimitResult from the check.

        Returns:
            Response dictionary for Flask.
        """
        response = jsonify(
            {
                "error": "Too Many Requests",
                "message": f"Rate limit exceeded. Try again in {result.retry_after or result.reset_after:.1f} seconds.",
                "retry_after": result.retry_after or result.reset_after,
            }
        )
        response.status_code = 429
        response.headers["X-RateLimit-Limit"] = str(result.limit)
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)
        response.headers["X-RateLimit-Reset"] = str(result.reset_after)
        response.headers["Retry-After"] = str(result.retry_after or result.reset_after)
        return response

    def _add_rate_limit_headers(self, response) -> dict:
        """Add rate limit headers to a response.

        This method is called by Flask's after_request hook.

        Args:
            response: The response to add headers to.

        Returns:
            The modified response.
        """
        rate_limit_result = getattr(g, "rate_limit_result", None)
        if rate_limit_result is not None:
            response.headers["X-RateLimit-Limit"] = str(rate_limit_result.limit)
            response.headers["X-RateLimit-Remaining"] = str(rate_limit_result.remaining)
            response.headers["X-RateLimit-Reset"] = str(rate_limit_result.reset_after)

        return response


class RateLimitConfig:
    """Configuration helper for rate limiting.

    Provides a convenient way to configure rate limiting for Flask.

    Example:
        ```python
        from flask import Flask
        from rate_limiter.middleware.flask import RateLimitConfig, RateLimitExtension

        app = Flask(__name__)

        # Configure rate limiting
        config = RateLimitConfig()
        config.add_rule(limit=100, window=60, key_type="ip")
        config.add_rule(limit=1000, window=3600, key_type="user_id")

        # Initialize extension
        rate_limiter = RateLimitExtension(app, config=config.get_config())
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
        """Get the RateLimiterConfig for use with extension.

        Returns:
            Configured RateLimiterConfig instance.
        """
        return RateLimiterConfig(rules=self.rules)


# Convenience decorator for rate limiting specific Flask routes
def rate_limit(
    limit: int,
    window: int,
    algorithm: str = "token_bucket",
    key_type: str = "ip",
    limiter: Optional[RateLimiter] = None,
):
    """Decorator to apply rate limiting to Flask routes.

    Args:
        limit: Maximum number of requests allowed in the window.
        window: Time window in seconds.
        algorithm: Rate limiting algorithm to use.
        key_type: Type of key to use ('ip', 'user_id', 'path', 'method').
        limiter: Optional RateLimiter instance.

    Returns:
        Decorated route function.

    Example:
        ```python
        from flask import Flask
        from rate_limiter.middleware.flask import rate_limit

        app = Flask(__name__)

        @app.route("/api/limited")
        @rate_limit(limit=10, window=60, key_type="ip")
        def limited_endpoint():
            return {"message": "This endpoint is rate limited"}
        ```
    """
    from functools import wraps

    from ..config import Algorithm, RateLimitRule, RateLimiterConfig

    # Convert algorithm string to enum
    try:
        algo = Algorithm(algorithm.lower())
    except ValueError:
        raise ValueError(
            f"Unknown algorithm: {algorithm}. "
            f"Valid options: {', '.join(a.value for a in Algorithm)}"
        )

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Create limiter if not provided
            if limiter is None:
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

                rule = RateLimitRule(limit=limit, window=window, algorithm=algo, key_func=key_func)
                config = RateLimiterConfig(rules=[rule])
                local_limiter = RateLimiter(config)
            else:
                local_limiter = limiter
                key_func = None

            # Check rate limit
            result = local_limiter.check_request(request)

            if not result.allowed:
                # Return 429 response
                response = jsonify(
                    {
                        "error": "Too Many Requests",
                        "message": f"Rate limit exceeded. Try again in {result.retry_after or result.reset_after:.1f} seconds.",
                    }
                )
                response.status_code = 429
                return response

            # Call the original function
            return func(*args, **kwargs)

        return wrapper

    return decorator


# Convenience function to create a rate limited Flask app
def create_rate_limited_app(
    app: Flask,
    limit: int = 100,
    window: int = 60,
    algorithm: str = "token_bucket",
    key_type: str = "ip",
) -> RateLimitExtension:
    """Create a rate limited Flask application.

    Args:
        app: Flask application instance.
        limit: Maximum number of requests per window (default: 100).
        window: Time window in seconds (default: 60).
        algorithm: Rate limiting algorithm (default: "token_bucket").
        key_type: Key type for rate limiting (default: "ip").

    Returns:
        RateLimitExtension instance.

    Example:
        ```python
        from flask import Flask
        from rate_limiter.middleware.flask import create_rate_limited_app

        app = Flask(__name__)

        # Add rate limiting
        rate_limiter = create_rate_limited_app(
            app,
            limit=100,
            window=60,
            key_type="ip"
        )
        ```
    """
    config = RateLimitConfig()
    config.add_rule(limit=limit, window=window, algorithm=algorithm, key_type=key_type)
    return RateLimitExtension(app, config=config.get_config())