# Tasks: Rate Limiter

## Task 1: Project Setup and Base Interfaces

- [x] 1.1 Create `pyproject.toml` with project metadata, dependencies (redis, pytest, flask, fastapi, uvicorn, httpx), and build configuration
- [x] 1.2 Create `src/rate_limiter/__init__.py` with public API exports
- [x] 1.3 Create `src/rate_limiter/algorithms/base.py` with `BaseAlgorithm` abstract class and `RateLimitResult` dataclass
- [x] 1.4 Create `src/rate_limiter/storage/base.py` with `BaseStorage` abstract class defining get, set, increment, and execute_atomic methods
- [x] 1.5 Create `src/rate_limiter/config.py` with `Algorithm` enum, `RateLimitRule` dataclass, and `RateLimiterConfig` dataclass

## Task 2: In-Memory Storage Backend

- [x] 2.1 Create `src/rate_limiter/storage/__init__.py` with storage exports
- [x] 2.2 Implement `src/rate_limiter/storage/memory.py` with `MemoryStorage` class using threading locks for thread safety, TTL support via expiry timestamps, and atomic execute method
- [x] 2.3 Create `tests/test_storage/__init__.py` and `tests/test_storage/test_memory.py` with unit tests for get/set/increment/TTL expiry/thread safety

## Task 3: Redis Storage Backend

- [x] 3.1 Implement `src/rate_limiter/storage/redis.py` with `RedisStorage` class wrapping redis-py, supporting get/set/increment via pipelines, and execute_atomic via Lua script evaluation
- [x] 3.2 Create `tests/test_storage/test_redis.py` with unit tests using fakeredis or mocked redis for get/set/increment/Lua script execution

## Task 4: Token Bucket Algorithm

- [x] 4.1 Create `src/rate_limiter/algorithms/__init__.py` with algorithm exports
- [x] 4.2 Implement `src/rate_limiter/algorithms/token_bucket.py` with `TokenBucketAlgorithm` class — tracks tokens and last refill time, refills based on elapsed time, consumes one token per request
- [x] 4.3 Write Lua script string constant in token_bucket.py for atomic Redis token bucket operations
- [x] 4.4 Create `tests/test_algorithms/__init__.py` and `tests/test_algorithms/test_token_bucket.py` with tests for: allow when tokens available, deny when empty, refill over time, burst handling

## Task 5: Leaking Bucket Algorithm

- [x] 5.1 Implement `src/rate_limiter/algorithms/leaking_bucket.py` with `LeakingBucketAlgorithm` class — maintains a queue counter that drains at a fixed rate, rejects when queue is full
- [x] 5.2 Write Lua script for atomic Redis leaking bucket operations
- [x] 5.3 Create `tests/test_algorithms/test_leaking_bucket.py` with tests for: allow when queue has space, deny when full, drain over time

## Task 6: Fixed Window Counter Algorithm

- [x] 6.1 Implement `src/rate_limiter/algorithms/fixed_window.py` with `FixedWindowAlgorithm` class — increments counter for current time window, resets at window boundary
- [x] 6.2 Write Lua script for atomic Redis fixed window operations
- [x] 6.3 Create `tests/test_algorithms/test_fixed_window.py` with tests for: counting within window, reset at boundary, deny at limit

## Task 7: Sliding Window Log Algorithm

- [x] 7.1 Implement `src/rate_limiter/algorithms/sliding_window_log.py` with `SlidingWindowLogAlgorithm` class — stores timestamp of each request, removes expired entries, counts remaining
- [x] 7.2 Write Lua script for atomic Redis sliding window log operations (ZADD + ZREMRANGEBYSCORE + ZCARD)
- [x] 7.3 Create `tests/test_algorithms/test_sliding_window_log.py` with tests for: allow within limit, deny at limit, old entries expire

## Task 8: Sliding Window Counter Algorithm

- [x] 8.1 Implement `src/rate_limiter/algorithms/sliding_window_counter.py` with `SlidingWindowCounterAlgorithm` class — weighted sum of current window count and previous window count based on overlap percentage
- [x] 8.2 Write Lua script for atomic Redis sliding window counter operations
- [x] 8.3 Create `tests/test_algorithms/test_sliding_window_counter.py` with tests for: weighted calculation accuracy, window transitions, boundary behavior

## Task 9: Key Resolution

- [x] 9.1 Implement `src/rate_limiter/keys.py` with `ip_key` (handles X-Forwarded-For), `user_id_key`, and `composite_key` factory function
- [x] 9.2 Create `tests/test_keys.py` with tests for IP extraction (direct and proxied), user ID extraction, and composite key generation

## Task 10: Core RateLimiter Orchestrator

- [x] 10.1 Implement `src/rate_limiter/core.py` with `RateLimiter` class — initializes storage and algorithms, provides `check()` for single rule and `check_request()` for all applicable rules, supports `update_rules()` for dynamic reconfiguration, handles fail-open/fail-closed on storage errors
- [x] 10.2 Create `tests/test_core.py` with tests for: single rule check, multiple rules (most restrictive wins), dynamic rule update, storage backend selection, fail-open/fail-closed behavior, plain string key support

## Task 11: Decorator Integration

- [x] 11.1 Implement `src/rate_limiter/decorators.py` with `rate_limit` decorator — resolves key, checks limit, raises appropriate exception or returns 429 response on denial
- [x] 11.2 Create `tests/test_decorators.py` with tests for: basic function decoration, rate limit enforcement, custom key function, proper exception on denial

## Task 12: FastAPI Middleware

- [x] 12.1 Create `src/rate_limiter/middleware/__init__.py` with middleware exports
- [x] 12.2 Implement `src/rate_limiter/middleware/fastapi.py` with `RateLimitMiddleware` (ASGI) — checks all applicable rules, returns 429 JSONResponse with headers on denial, adds rate limit headers to successful responses
- [x] 12.3 Create `tests/test_middleware/__init__.py` and `tests/test_middleware/test_fastapi.py` with tests using httpx/TestClient for: allowed requests get headers, denied requests get 429, multiple rules applied
## Task 13: Flask Middleware

- [x] 13.1 Implement `src/rate_limiter/middleware/flask.py` with `RateLimitExtension` class — registers before_request and after_request hooks, returns 429 with headers on denial
- [x] 13.2 Create `tests/test_middleware/test_flask.py` with tests using Flask test client for: allowed requests, denied requests with 429 and correct headers

## Task 14: Examples and Documentation

- [x] 14.1 Create `examples/basic_usage.py` demonstrating in-memory rate limiting with decorator
- [x] 14.2 Create `examples/fastapi_example.py` demonstrating FastAPI app with rate limit middleware and multiple rules
- [x] 14.3 Create `examples/flask_example.py` demonstrating Flask app with rate limit extension
- [x] 14.4 Create `README.md` with installation instructions, quick start, configuration reference, algorithm descriptions, and integration guides


## 2026-10-27 chapter re-review

- [x] Read original chapter923–1507 and use graph impact analysis.
- [x] Apply path/key selectors, isolate stable identities, snapshot configuration.
- [x] Fix regression clocks, leak occupancy/retry and validate storage replies.
- [x] Add independent bucket capacity, strict file reloads and fixed metrics.
- [x] Bound memory/Redis resources; disable duplicate-charge automatic retries.
- [x] Own and close FIFO/polling workers; share HTTP response policies.
- [x] Run249 tests, real4-process Redis /2HTTP experiments and both demos.
- [x] Keep baseline/final/raw measurements and document throughput regression.
- [x] Update bilingual README/design/requirements, chapter mapping and root status.
