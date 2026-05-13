"""Rate Limiter - A Python rate limiting library.

Supports multiple algorithms, configurable rules, and both local (in-memory)
and distributed (Redis) backends. Provides decorator and middleware integration
for Flask/FastAPI with proper HTTP 429 responses and rate limit headers.
"""

__version__ = "0.1.0"

# Core classes
from .core import RateLimiter  # noqa: F401

# Configuration
from .config import Algorithm, RateLimitRule, RateLimiterConfig  # noqa: F401

# Decorators
from .decorators import rate_limit  # noqa: F401

# Key resolvers
from .keys import ip_key, user_id_key, composite_key  # noqa: F401

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
