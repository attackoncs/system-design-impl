# Requirements: Rate Limiter

## Overview

A Python rate limiter library supporting multiple algorithms, configurable rules, and both local (in-memory) and distributed (Redis) backends. Flask/FastAPI adapters produce HTTP denial responses and rate limit headers; the general decorator raises an exception by default or returns a response dictionary when configured.

## Functional Requirements

### FR-1: Multiple Rate Limiting Algorithms

The system shall support the following rate limiting algorithms, selectable per rule:

- **FR-1.1**: Token Bucket — allows bursts up to bucket capacity, refills at a fixed rate. Suitable for APIs that tolerate short bursts.
- **FR-1.2**: Leaking Bucket — drain virtual occupancy at a fixed rate and immediately allow or reject a whole request. Actual FIFO pacing is provided separately by the optional, process-local `LeakyBucketQueue`; the admission algorithm does not schedule HTTP execution.
- **FR-1.3**: Fixed Window Counter — counts requests in fixed time windows (e.g., per minute). Simple and memory-efficient but can allow up to 2x the limit at window boundaries.
- **FR-1.4**: Sliding Window Log — retain a timestamp and unique member for each accepted request in the rolling window. Denied requests are not appended.
- **FR-1.5**: Sliding Window Counter — approximates sliding window using weighted current + previous window counts. Balances accuracy and memory efficiency.

### FR-2: Storage Backends

- **FR-2.1**: The system shall provide an in-memory storage backend for single-process deployments, using thread-safe data structures.
- **FR-2.2**: The system shall provide a Redis storage backend for distributed deployments, using a centralized Redis store for synchronization across multiple rate limiter instances.
- **FR-2.3**: Storage backends shall be interchangeable without changing rate limiting logic (interface-based abstraction).

### FR-3: Configuration-Driven Rules

- **FR-3.1**: Rate limiting rules shall be defined in configuration (Python dataclasses).
- **FR-3.2**: Each rule shall specify: identifier key type (IP, user ID, custom), algorithm, limit, and time window.
- **FR-3.3**: Rules shall be dynamically updatable at runtime without restart via `update_rules()`.
- **FR-3.4**: Multiple rules can apply to the same endpoint (e.g., per-IP and per-user limits). Evaluate matching rules sequentially; any denial wins with the longest retry delay. There is no group rollback of quota already charged by earlier rules.
- **FR-3.5**: Each rule may optionally specify a path pattern for endpoint-specific limiting.
- **FR-3.6**: Each rule may optionally specify a human-readable name for identification.

### FR-4: Integration Patterns

- **FR-4.1**: The system shall provide a Python decorator (`@rate_limit`) for function-level limiting.
- **FR-4.2**: The system shall provide ASGI middleware compatible with FastAPI/Starlette.
- **FR-4.3**: The system shall provide a Flask extension with before_request/after_request hooks.
- **FR-4.4**: Integration shall automatically extract client identifiers (IP, user ID) from requests.

### FR-5: HTTP Response Handling

- **FR-5.1**: Framework adapters default to HTTP 429 (Too Many Requests) on denial and allow a configured 4xx/5xx status. The general decorator raises by default; `raise_exception=False` returns a dictionary, not a framework HTTP response.
- **FR-5.2**: Responses shall include `X-RateLimit-Remaining` header with remaining request count.
- **FR-5.3**: Responses shall include `X-RateLimit-Limit` header with the maximum allowed requests.
- **FR-5.4**: Denial responses shall include `Retry-After` as a positive integer delay in seconds when headers are enabled.
- **FR-5.5**: Responses shall include `X-RateLimit-Reset` as a rounded-up relative algorithm reset duration when headers are enabled.
- **FR-5.6**: Both `include_headers` and `auto_headers` shall control rate limit headers on allowed and denied responses.

### FR-6: Throttle Key Resolution

- **FR-6.1**: Use the connection peer IP by default. Reading X-Forwarded-For requires explicit `trust_forwarded=True` behind a trusted proxy that sanitizes forwarded headers.
- **FR-6.2**: Support an authenticated principal supplied by the application or a custom resolver. Credential-derived keys use stable hashes; the limiter does not authenticate JWTs, API keys or sessions.
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
- **NFR-2.2**: Steady-state Redis checks use one EVALSHA per applicable rule; cold/cache-loss recovery adds SCRIPT LOAD and another EVALSHA. Requests may match multiple rules.
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

## 2026-10-31 acceptance additions

- String subjects of applicable rules must obey the same 1..8192-character bound
  in both check APIs; invalid inputs must not charge quota.
- Excessively nested rule JSON must preserve the active rules, record an error,
  and allow later valid reloads without restarting the worker.
- Script objects must be bounded per storage instance. NOSCRIPT recovery must
  preserve quota and atomic admission; timeouts/connection errors must not replay
  the mutation. Redis ACL requirements and cold-cache round trips must be documented.
- GitHub references, chapter coverage and outstanding deployment/monitoring
  boundaries must be recorded in the [review](open-source-review_cn.md).
