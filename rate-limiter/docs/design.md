# Design: Rate Limiter

## Architecture Overview

The rate limiter follows a layered architecture with clear separation between algorithms, storage, configuration, and integration layers.

```
┌─────────────────────────────────────────────────┐
│           Integration Layer                      │
│   (Decorators, FastAPI Middleware, Flask MW)     │
├─────────────────────────────────────────────────┤
│           Rate Limiter Core                      │
│   (RateLimiter class — orchestrates checks)     │
├─────────────────────────────────────────────────┤
│           Algorithm Layer                        │
│   (TokenBucket, LeakingBucket, FixedWindow,     │
│    SlidingWindowLog, SlidingWindowCounter)       │
├─────────────────────────────────────────────────┤
│           Storage Layer                          │
│   (MemoryBackend, RedisBackend)                 │
├─────────────────────────────────────────────────┤
│           Configuration                          │
│   (Rules, limits, key resolvers)                │
└─────────────────────────────────────────────────┘
```

## Project Structure

```
rate_limiter/
├── pyproject.toml
├── README.md
├── src/
│   └── rate_limiter/
│       ├── __init__.py
│       ├── core.py              # RateLimiter orchestrator
│       ├── config.py            # Rule definitions, configuration loading
│       ├── algorithms/
│       │   ├── __init__.py
│       │   ├── base.py          # Abstract base algorithm
│       │   ├── token_bucket.py
│       │   ├── leaking_bucket.py
│       │   ├── fixed_window.py
│       │   ├── sliding_window_log.py
│       │   └── sliding_window_counter.py
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── base.py          # Abstract base storage
│       │   ├── memory.py        # In-memory thread-safe storage
│       │   └── redis.py         # Redis storage with Lua scripts
│       ├── middleware/
│       │   ├── __init__.py
│       │   ├── fastapi.py       # ASGI middleware for FastAPI
│       │   └── flask.py         # WSGI middleware/extension for Flask
│       ├── decorators.py        # @rate_limit decorator
│       └── keys.py              # Key resolver functions
├── tests/
│   ├── __init__.py
│   ├── test_algorithms/
│   │   ├── __init__.py
│   │   ├── test_token_bucket.py
│   │   ├── test_leaking_bucket.py
│   │   ├── test_fixed_window.py
│   │   ├── test_sliding_window_log.py
│   │   └── test_sliding_window_counter.py
│   ├── test_storage/
│   │   ├── __init__.py
│   │   ├── test_memory.py
│   │   └── test_redis.py
│   ├── test_core.py
│   ├── test_decorators.py
│   ├── test_middleware/
│   │   ├── __init__.py
│   │   ├── test_fastapi.py
│   │   └── test_flask.py
│   └── test_keys.py
└── examples/
    ├── basic_usage.py
    ├── fastapi_example.py
    └── flask_example.py
```

## Component Design

### 1. Algorithm Base Class

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class RateLimitResult:
    """Result of a rate limit check."""
    allowed: bool
    remaining: int
    limit: int
    reset_after: float                      # seconds until the limit resets
    retry_after: Optional[float] = None     # seconds to wait before retrying (None if allowed)


class BaseAlgorithm(ABC):
    """Abstract base for rate limiting algorithms."""

    @abstractmethod
    def check(self, key: str, rule: "RateLimitRule", storage: "BaseStorage") -> RateLimitResult:
        """Check if a request should be allowed.
        
        Args:
            key: Unique identifier for the rate limit subject
            rule: The rate limiting rule to apply
            storage: Storage backend to use for state
            
        Returns:
            RateLimitResult indicating if the request is allowed
        """
        ...
```

### 2. Storage Base Class

```python
from abc import ABC, abstractmethod
from typing import Any, Optional


class BaseStorage(ABC):
    """Abstract base for storage backends."""

    @abstractmethod
    def get(self, key: str) -> Optional[Any]:
        """Get value for key."""
        ...

    @abstractmethod
    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Set value for key with optional TTL in seconds."""
        ...

    @abstractmethod
    def increment(self, key: str, amount: int = 1, ttl: Optional[float] = None) -> int:
        """Atomically increment a counter, returning new value."""
        ...

    @abstractmethod
    def execute_atomic(self, script: str, keys: list[str], args: list[Any]) -> Any:
        """Execute an atomic operation (Lua script for Redis, lock for memory)."""
        ...
```

### 3. Configuration Model

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class Algorithm(Enum):
    TOKEN_BUCKET = "token_bucket"
    LEAKING_BUCKET = "leaking_bucket"
    FIXED_WINDOW = "fixed_window"
    SLIDING_WINDOW_LOG = "sliding_window_log"
    SLIDING_WINDOW_COUNTER = "sliding_window_counter"


@dataclass
class RateLimitRule:
    """A single rate limiting rule."""
    limit: int                              # max requests
    window: int                             # time window in seconds
    algorithm: Algorithm = Algorithm.TOKEN_BUCKET
    key_func: Optional[Callable] = None     # custom key extractor
    key_type: str = "ip"                    # "ip", "user_id", "custom"
    path_pattern: Optional[str] = None      # specific endpoint pattern, None = all
    name: Optional[str] = None              # human-readable rule name


@dataclass
class RateLimiterConfig:
    """Top-level configuration."""
    rules: list[RateLimitRule] = field(default_factory=list)
    storage_backend: str = "memory"         # "memory" or "redis"
    redis_url: str = "redis://localhost:6379"
    key_prefix: str = "rl:"                 # prefix for storage keys
    default_response_code: int = 429
    include_headers: bool = True
    fail_open: bool = True                  # allow requests when storage fails
```

### 4. Core RateLimiter Class

```python
class RateLimiter:
    """Main orchestrator that ties algorithms, storage, and config together."""

    def __init__(self, config: RateLimiterConfig):
        self._config = config
        self._storage = self._create_storage(config)
        self._algorithms: dict[Algorithm, BaseAlgorithm] = self._load_algorithms()

    def check(self, key: str, rule: RateLimitRule) -> RateLimitResult:
        """Check a request against a specific rule.
        
        Handles storage failures based on fail_open config.
        """
        algorithm = self._algorithms[rule.algorithm]
        try:
            return algorithm.check(key, rule, self._storage)
        except Exception:
            if self._config.fail_open:
                return RateLimitResult(allowed=True, ...)
            else:
                return RateLimitResult(allowed=False, ...)

    def check_request(self, request_context, rules=None) -> RateLimitResult:
        """Check a request against all applicable rules.
        
        Returns the most restrictive result (first denial, or lowest remaining).
        Supports plain string keys or request objects for key resolution.
        """
        ...

    def update_rules(self, rules: list[RateLimitRule]) -> None:
        """Dynamically update rules at runtime."""
        self._config.rules = rules
```

### 5. Redis Lua Scripts (Race Condition Handling)

Each algorithm has a corresponding Lua script for atomic Redis operations:

**Token Bucket Lua Script (conceptual):**
```lua
-- KEYS[1] = bucket key
-- ARGV[1] = capacity, ARGV[2] = refill_rate, ARGV[3] = current_time
local tokens = tonumber(redis.call('hget', KEYS[1], 'tokens') or ARGV[1])
local last_refill = tonumber(redis.call('hget', KEYS[1], 'last_refill') or ARGV[3])
local elapsed = tonumber(ARGV[3]) - last_refill
local refilled = math.min(tonumber(ARGV[1]), tokens + elapsed * tonumber(ARGV[2]))
if refilled >= 1 then
    redis.call('hset', KEYS[1], 'tokens', refilled - 1)
    redis.call('hset', KEYS[1], 'last_refill', ARGV[3])
    redis.call('expire', KEYS[1], math.ceil(tonumber(ARGV[1]) / tonumber(ARGV[2])) + 1)
    return {1, math.floor(refilled - 1), tonumber(ARGV[1])}
else
    return {0, 0, tonumber(ARGV[1])}
end
```

### 6. Decorator Design

```python
from functools import wraps
from typing import Callable, Optional


def rate_limit(
    limit: int,
    window: float,
    algorithm: str = "token_bucket",
    key_func: Optional[Callable] = None,
    limiter: Optional["RateLimiter"] = None,
):
    """Decorator to apply rate limiting to a function or route handler.
    
    Usage:
        @rate_limit(limit=100, window=60)
        def my_endpoint():
            ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Resolve key from context
            # Check rate limit
            # If denied, raise/return 429
            # If allowed, call func
            ...
        return wrapper
    return decorator
```

### 7. FastAPI Middleware

```python
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class RateLimitMiddleware(BaseHTTPMiddleware):
    """ASGI middleware for FastAPI rate limiting."""

    def __init__(self, app, limiter: "RateLimiter"):
        super().__init__(app)
        self.limiter = limiter

    async def dispatch(self, request: Request, call_next):
        # Resolve key(s) from request
        # Check all applicable rules
        # If denied: return 429 with headers
        # If allowed: proceed and add headers to response
        ...
```

### 8. Key Resolution

```python
from typing import Callable, Optional


def ip_key(request) -> str:
    """Extract client IP from request (handles proxies via X-Forwarded-For)."""
    ...


def user_id_key(request) -> str:
    """Extract authenticated user ID from request."""
    ...


def composite_key(*extractors: Callable) -> Callable:
    """Combine multiple key extractors into a composite key."""
    def resolver(request) -> str:
        parts = [extractor(request) for extractor in extractors]
        return ":".join(parts)
    return resolver
```

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Algorithm selection | Per-rule configuration | Different endpoints may need different algorithms (burst-friendly vs strict) |
| Storage abstraction | Interface-based | Allows swapping memory/Redis without algorithm changes |
| Redis atomicity | Lua scripts | Prevents race conditions in distributed environments (single round-trip) |
| Distributed sync | Centralized Redis | Multiple rate limiter instances share state via Redis, avoiding sticky sessions |
| Thread safety (memory) | threading.Lock per operation | Balances safety with performance for in-memory use |
| Key resolution | Pluggable functions | Maximum flexibility for custom throttle keys |
| Middleware approach | Both ASGI and WSGI | Covers FastAPI and Flask ecosystems |
| Header format | X-RateLimit-* + Retry-After | Industry standard (GitHub, Stripe pattern) |
| Fault tolerance | Configurable fail-open/fail-closed | Allows operators to choose availability vs safety |
| Sliding window counter keys | Window-start-time-based | Ensures correct window transitions without explicit state migration |

## Error Handling

- **Redis connection failure**: Configurable behavior via `fail_open` config — either fail-open (allow request, default) or fail-closed (deny request). Failures are logged as warnings.
- **Redis timeout**: Connection and socket timeouts are set to 2 seconds to prevent blocking.
- **Invalid configuration**: Raise `ValueError` at initialization time with descriptive message.
- **Missing key resolver**: Fall back to IP-based key resolution.
- **Plain string request context**: When `check_request` receives a plain string instead of a request object, it uses the string directly as the rate limit key.
