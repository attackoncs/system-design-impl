# Rate Limiter

A production-quality Python rate limiting library supporting multiple algorithms, configurable rules, and both local (in-memory) and distributed (Redis) backends. Provides decorator and middleware integration for Flask and FastAPI with proper HTTP 429 responses and rate limit headers.

> 中文文档请参阅 [README_CN.md](./README_CN.md)

## Features

- **5 rate limiting algorithms**: Token Bucket, Leaking Bucket, Fixed Window, Sliding Window Log, Sliding Window Counter
- **2 storage backends**: In-memory (single process) and Redis (distributed)
- **Framework integration**: FastAPI middleware, Flask extension, and a universal decorator
- **Flexible key resolution**: IP-based, user ID, composite keys, and custom functions
- **Multiple rules per endpoint**: Most restrictive result wins
- **Dynamic rule updates**: Change rules at runtime without restart
- **Fault tolerance**: Configurable fail-open or fail-closed behavior
- **HTTP 429 responses**: Proper rate limit headers (`X-RateLimit-Remaining`, `Retry-After`, etc.)

## Architecture

```
┌──────────────────────┐   ┌───────────────────┐   ┌───────────────────────┐
│   Integration Layer  │   │    Core Layer      │   │   Algorithm Layer     │
│                      │   │                    │   │                       │
│ • @rate_limit        │──▶│ • RateLimiter      │──▶│ • TokenBucket         │
│ • FastAPI Middleware │   │ • Rule Engine      │   │ • LeakingBucket       │
│ • Flask Extension    │   │ • Key Resolver     │   │ • FixedWindow         │
│                      │   │                    │   │ • SlidingWindowLog    │
└──────────────────────┘   └───────────────────┘   │ • SlidingWindowCounter│
                                                    └───────────┬───────────┘
                                                                │
                                                    ┌───────────▼───────────┐
                                                    │   Storage Layer       │
                                                    │                       │
                                                    │ • MemoryStorage       │
                                                    │ • RedisStorage        │
                                                    └───────────────────────┘
```

### Layer Responsibilities

| Layer | Components | Role |
|-------|-----------|------|
| Integration | Decorator, FastAPI Middleware, Flask Extension | User-facing API, framework hooks |
| Core | RateLimiter, Config, Key Resolver | Orchestration, rule evaluation, key resolution |
| Algorithm | 5 algorithm implementations | Rate limit check logic, state management |
| Storage | Memory, Redis | Persistent state, atomic operations |

## Installation

```bash
pip install -e .
```

With development dependencies:

```bash
pip install -e ".[dev]"
```

### Requirements

- Python >= 3.9
- Redis (only if using the Redis storage backend)

## Quick Start

```python
from rate_limiter import RateLimiter, RateLimiterConfig, RateLimitRule, Algorithm

# Define rules
rules = [
    RateLimitRule(limit=100, window=60, algorithm=Algorithm.TOKEN_BUCKET),
]

# Create the rate limiter
config = RateLimiterConfig(rules=rules)
limiter = RateLimiter(config)

# Check a request
result = limiter.check_request("user-123")

if result.allowed:
    print(f"Request allowed. {result.remaining} remaining.")
else:
    print(f"Rate limited. Retry after {result.retry_after} seconds.")
```

### Using the Decorator

```python
from rate_limiter import rate_limit

@rate_limit(limit=10, window=60, algorithm="token_bucket")
def my_endpoint():
    return "Hello, world!"
```

## Configuration Reference

### RateLimiterConfig

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `rules` | `list[RateLimitRule]` | `[]` | Rate limiting rules to apply |
| `storage_backend` | `str` | `"memory"` | `"memory"` or `"redis"` |
| `redis_url` | `str` | `"redis://localhost:6379"` | Redis connection URL |
| `key_prefix` | `str` | `"rl:"` | Storage key prefix |
| `include_headers` | `bool` | `True` | Include rate limit headers in responses |
| `fail_open` | `bool` | `True` | Allow requests when storage is unavailable |

### RateLimitRule

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | `int` | *(required)* | Max requests in the window |
| `window` | `int` | *(required)* | Time window in seconds |
| `algorithm` | `Algorithm` | `TOKEN_BUCKET` | Algorithm to use |
| `key_func` | `Callable` | `None` | Custom key resolver |
| `key_type` | `str` | `"ip"` | `"ip"`, `"user_id"`, or `"custom"` |
| `path_pattern` | `str` | `None` | Endpoint path pattern |
| `name` | `str` | `None` | Human-readable rule name |

## Algorithms

### Token Bucket

Maintains a bucket of tokens that refills at a steady rate. Each request consumes one token. Allows bursts up to bucket capacity.

- **State**: `tokens` (current count) + `last_refill` (timestamp)
- **Pros**: Handles burst traffic gracefully
- **Cons**: Two values to track
- **Best for**: APIs that tolerate short bursts

### Leaking Bucket

Maintains a queue that drains at a fixed rate. Rejects when the queue is full.

- **State**: `queue_count` + `last_drain` (timestamp)
- **Pros**: Guarantees steady output rate
- **Cons**: Does not accommodate bursts
- **Best for**: Systems requiring stable, predictable throughput

### Fixed Window Counter

Divides time into fixed windows, counts requests per window. Resets at boundaries.

- **State**: `window_start` + `counter`
- **Pros**: Simple, memory-efficient
- **Cons**: Can allow 2x limit at window boundaries
- **Best for**: Simple use cases where boundary spikes are acceptable

### Sliding Window Log

Tracks exact timestamp of each request. Removes expired entries, counts the rest.

- **State**: Sorted set of timestamps
- **Pros**: Most accurate — no boundary issues
- **Cons**: Higher memory (stores every timestamp)
- **Best for**: Strict rate limiting where precision matters

### Sliding Window Counter

Weighted sum of current and previous window counts. Approximates a true sliding window.

- **State**: Two counters (current + previous window), keyed by window start time
- **Pros**: Good accuracy with low memory
- **Cons**: Approximation (assumes even distribution in previous window)
- **Best for**: Balancing accuracy and resource efficiency

## Integration Guides

### FastAPI Middleware

```python
from fastapi import FastAPI
from rate_limiter import RateLimiter, RateLimiterConfig, RateLimitRule, Algorithm
from rate_limiter.middleware.fastapi import RateLimitMiddleware

app = FastAPI()

rules = [
    RateLimitRule(limit=100, window=60, algorithm=Algorithm.TOKEN_BUCKET),
    RateLimitRule(limit=1000, window=3600, algorithm=Algorithm.SLIDING_WINDOW_COUNTER),
]
config = RateLimiterConfig(rules=rules)
limiter = RateLimiter(config)

app.add_middleware(RateLimitMiddleware, limiter=limiter)

@app.get("/api/data")
async def get_data():
    return {"message": "Hello"}
```

Rate-limited responses:

```
HTTP/1.1 429 Too Many Requests
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 45.2
Retry-After: 45.2
```

### Flask Extension

```python
from flask import Flask
from rate_limiter import RateLimiter, RateLimiterConfig, RateLimitRule, Algorithm
from rate_limiter.middleware.flask import RateLimitExtension

app = Flask(__name__)

rules = [RateLimitRule(limit=100, window=60, algorithm=Algorithm.TOKEN_BUCKET)]
config = RateLimiterConfig(rules=rules)
limiter = RateLimiter(config)

rate_ext = RateLimitExtension(app, limiter=limiter)

@app.route("/api/data")
def get_data():
    return {"message": "Hello"}
```

### Decorator

```python
from rate_limiter import rate_limit
from rate_limiter.decorators import RateLimitExceededException

@rate_limit(limit=5, window=60, algorithm="sliding_window_counter")
def process_order():
    return "Order processed"

try:
    result = process_order()
except RateLimitExceededException as e:
    print(f"Rate limited: retry after {e.retry_after}s")
```

## Key Resolution

### Built-in Key Functions

| Function | Description |
|----------|-------------|
| `ip_key` | Client IP (handles `X-Forwarded-For`) |
| `user_id_key` | User identity (JWT, API key, user object) |
| `composite_key` | Combines multiple key functions |
| `path_key` | Request path |
| `method_key` | HTTP method |

### Custom Key Functions

```python
def tenant_key(request):
    return f"tenant:{request.headers.get('X-Tenant-ID', 'default')}"

rule = RateLimitRule(limit=100, window=60, key_func=tenant_key)
```

## Storage Backends

### Memory (Default)

Thread-safe in-process storage. No external dependencies.

```python
config = RateLimiterConfig(storage_backend="memory")
```

- Atomic operations via `threading.Lock`
- TTL-based expiry
- State lost on restart

### Redis (Distributed)

Centralized storage for multi-instance deployments.

```python
config = RateLimiterConfig(storage_backend="redis", redis_url="redis://localhost:6379")
```

- Atomic operations via Lua scripts (no race conditions)
- Shared state across processes/servers
- Connection timeout: 2 seconds

### Fail-Open vs Fail-Closed

```python
# Fail-open (default): allow requests when storage is down
config = RateLimiterConfig(fail_open=True)

# Fail-closed: deny requests when storage is down
config = RateLimiterConfig(fail_open=False)
```

## Extensibility

### Adding a New Algorithm

```python
from rate_limiter.algorithms.base import BaseAlgorithm, RateLimitResult

class CustomAlgorithm(BaseAlgorithm):
    def check(self, key, rule, storage):
        # Your logic here
        return RateLimitResult(allowed=True, remaining=99, limit=100, reset_after=60.0, retry_after=None)
```

### Adding a New Storage Backend

```python
from rate_limiter.storage.base import BaseStorage

class CustomStorage(BaseStorage):
    def get(self, key): ...
    def set(self, key, value, ttl=None): ...
    def increment(self, key, amount=1, ttl=None): ...
    def execute_atomic(self, script, keys, args): ...
```

## Performance

| Algorithm | Time Complexity | Space per Key |
|-----------|----------------|---------------|
| Token Bucket | O(1) | O(1) — 2 values |
| Leaking Bucket | O(1) | O(1) — 2 values |
| Fixed Window | O(1) | O(1) — 2 values |
| Sliding Window Log | O(n) | O(n) — all timestamps |
| Sliding Window Counter | O(1) | O(1) — 2 counters |

Redis adds one network round-trip per check (single Lua script evaluation).

## Running Tests

```bash
pip install -e ".[dev]"
pytest                          # All 180 tests
pytest tests/test_algorithms/   # Algorithm tests
pytest tests/test_storage/      # Storage tests
pytest tests/test_middleware/    # Middleware tests
pytest tests/test_core.py       # Core orchestrator tests
pytest tests/test_decorators.py # Decorator tests
```

## License

MIT
