# Requirements: Rate Limiter

## Overview

A Python rate limiter library supporting multiple algorithms, configurable rules, and both local (in-memory) and distributed (Redis) backends. Provides decorator and middleware integration for Flask/FastAPI with proper HTTP 429 responses and rate limit headers.

## Functional Requirements

### FR-1: Multiple Rate Limiting Algorithms

The system shall support the following rate limiting algorithms, selectable per rule:

- **FR-1.1**: Token Bucket — allows bursts up to bucket capacity, refills at a fixed rate. Suitable for APIs that tolerate short bursts.
- **FR-1.2**: Leaking Bucket — processes requests at a fixed rate, rejects overflow when queue is full. Suitable for stable outflow rate requirements.
- **FR-1.3**: Fixed Window Counter — counts requests in fixed time windows (e.g., per minute). Simple and memory-efficient but can allow up to 2x the limit at window boundaries.
- **FR-1.4**: Sliding Window Log — tracks exact timestamps of each request for precise limiting. Most accurate but higher memory usage.
- **FR-1.5**: Sliding Window Counter — approximates sliding window using weighted current + previous window counts. Balances accuracy and memory efficiency.

### FR-2: Storage Backends

- **FR-2.1**: The system shall provide an in-memory storage backend for single-process deployments, using thread-safe data structures.
- **FR-2.2**: The system shall provide a Redis storage backend for distributed deployments, using a centralized Redis store for synchronization across multiple rate limiter instances.
- **FR-2.3**: Storage backends shall be interchangeable without changing rate limiting logic (interface-based abstraction).

### FR-3: Configuration-Driven Rules

- **FR-3.1**: Rate limiting rules shall be defined in configuration (Python dataclasses).
- **FR-3.2**: Each rule shall specify: identifier key type (IP, user ID, custom), algorithm, limit, and time window.
- **FR-3.3**: Rules shall be dynamically updatable at runtime without restart via `update_rules()`.
- **FR-3.4**: Multiple rules can apply to the same endpoint (e.g., per-IP and per-user limits). The most restrictive result is returned.
- **FR-3.5**: Each rule may optionally specify a path pattern for endpoint-specific limiting.
- **FR-3.6**: Each rule may optionally specify a human-readable name for identification.

### FR-4: Integration Patterns

- **FR-4.1**: The system shall provide a Python decorator (`@rate_limit`) for function-level limiting.
- **FR-4.2**: The system shall provide ASGI middleware compatible with FastAPI/Starlette.
- **FR-4.3**: The system shall provide a Flask extension with before_request/after_request hooks.
- **FR-4.4**: Integration shall automatically extract client identifiers (IP, user ID) from requests.

### FR-5: HTTP Response Handling

- **FR-5.1**: When a request is rate limited, the system shall return HTTP 429 (Too Many Requests).
- **FR-5.2**: Responses shall include `X-RateLimit-Remaining` header with remaining request count.
- **FR-5.3**: Responses shall include `X-RateLimit-Limit` header with the maximum allowed requests.
- **FR-5.4**: Responses shall include `Retry-After` header with seconds until the client can retry.
- **FR-5.5**: Responses shall include `X-RateLimit-Reset` header with seconds until the window resets.
- **FR-5.6**: Rate limit headers shall be configurable (can be disabled via `include_headers` config).

### FR-6: Throttle Key Resolution

- **FR-6.1**: The system shall support throttling by client IP address (handles X-Forwarded-For for proxied requests).
- **FR-6.2**: The system shall support throttling by authenticated user ID (JWT, API key, session).
- **FR-6.3**: The system shall support throttling by custom properties via a configurable key function.
- **FR-6.4**: The system shall support composite keys combining multiple extractors (e.g., IP + endpoint path).
- **FR-6.5**: The system shall support throttling by request path and HTTP method.

### FR-7: Fault Tolerance

- **FR-7.1**: The system shall support configurable fail-open behavior: when storage (Redis) is unavailable, allow requests through (default).
- **FR-7.2**: The system shall support configurable fail-closed behavior: when storage is unavailable, deny requests.
- **FR-7.3**: Storage failures shall be logged as warnings without crashing the application.

## Non-Functional Requirements

### NFR-1: Race Condition Safety

- **NFR-1.1**: Redis operations shall use Lua scripts to ensure atomicity of check-and-increment, preventing race conditions in distributed environments.
- **NFR-1.2**: In-memory backend shall use threading locks to ensure thread safety for concurrent access.

### NFR-2: Performance

- **NFR-2.1**: In-memory rate limit checks target sub-1ms single-operation latency; report measured tails without a universal latency guarantee.
- **NFR-2.2**: Redis checks use one EVAL per applicable rule; requests matching multiple rules can require multiple round-trips.
- **NFR-2.3**: Redis connections shall use configurable timeouts to prevent blocking on connection failures.

### NFR-3: Extensibility

- **NFR-3.1**: New algorithms shall be addable by implementing the `BaseAlgorithm` abstract class.
- **NFR-3.2**: New storage backends shall be addable by implementing the `BaseStorage` abstract class.
- **NFR-3.3**: Custom key extraction functions shall be injectable via `key_func` parameter.

### NFR-4: Project Structure

- **NFR-4.1**: The project shall use standard Python packaging (pyproject.toml with hatchling).
- **NFR-4.2**: The project shall include type hints throughout.
- **NFR-4.3**: The project shall include unit tests using pytest.
- **NFR-4.4**: The project shall provide usage examples for basic, FastAPI, and Flask integrations.

## 2026-10-27 acceptance additions

Apply key_type and query-free path globs; copy rules and publish complete sets
atomically. Rule files are strict64KiB/100-rule JSON; invalid reloads retain the
last good configuration. Buckets support capacity independent of steady rate.
Sample time inside atomic storage, retain clock watermarks, use Redis TIME and
validate result contracts. Bound keys/cleanup/Redis connections/timeouts; disable
automatic non-idempotent command retries. Share HTTP bodies, integer retry headers,
header flags and configurable status. Trust forwarded IP only explicitly.
Expose fixed metrics and owned poller/FIFO workers; include real multiprocess
Redis evidence, measured performance regressions and migration notes. Logging
counts accepted requests; HA/global quotas/durable queues remain out of scope.
