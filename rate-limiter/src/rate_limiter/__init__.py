"""Rate Limiter - A Python rate limiting library.

Supports multiple algorithms, configurable rules, and both local (in-memory)
and distributed (Redis) backends. Provides decorator and middleware integration
for Flask/FastAPI with proper HTTP 429 responses and rate limit headers.
"""

__version__ = "0.1.0"

# Core classes
# Configuration
from rate_limiter.config import (  # noqa: F401
    Algorithm,
    RateLimiterConfig,
    RateLimitRule,
)
from rate_limiter.core import RateLimiter  # noqa: F401

# Decorators
from rate_limiter.decorators import rate_limit  # noqa: F401

# Key resolvers
from rate_limiter.keys import composite_key, ip_key, user_id_key  # noqa: F401

__all__ = [
    "__version__",
    "RateLimiter",
    "Algorithm",
    "RateLimitRule",
    "RateLimiterConfig",
    "rate_limit",
    "ip_key",
    "user_id_key",
    "composite_key",
]
