# Rate Limiter — Chapter 5

<!-- LEARNING_GUIDE_START -->
## Code reading guide

[Learn through a concrete example: architecture, state changes and annotated source (Chinese)](docs/learning-guide_cn.md).
<!-- LEARNING_GUIDE_END -->

A runnable study of five rate limiting algorithms, atomic memory/Redis storage,
request policy selection and Flask/FastAPI integration. [中文](README_CN.md).

The chapter review found missing policy selection and file reloads, clock
regression bugs, unstable cross-process identity hashes and inconsistent HTTP
responses. These are now covered by 249 tests and a separate real Redis probe.
See [coverage and migration](docs/chapter05-review.md), [design](docs/design.md)
and [measurements](benchmarks/REPORT.md).

## Run

Python 3.9+ source syntax; execution verified on Python 3.13.9. Runtime dependency:
redis-py (verified 6.4.0); frameworks and test tools are optional dev dependencies.

```sh
pip install -e ".[dev]"
python -m pytest tests -q
python examples/basic_usage.py
python examples/policy_demo.py
python tools/benchmark.py --output benchmarks/local.json
```

The review environment disables unrelated pytest plugin autoload, uses
`-p no:cacheprovider`, and selects a fresh `--basetemp` inside `.test-tmp` because
its existing default cache/temp directories are inaccessible. With installed
Starlette 1.8.0 and httpx, its TestClient emits a compatibility warning about
optional httpx2; the review uses `-W error` with only
`-W ignore::starlette.exceptions.StarletteDeprecationWarning`. No project tests
are skipped. These are environment workarounds, not application requirements.

## Configure and check

```python
from rate_limiter import Algorithm, RateLimiter, RateLimiterConfig, RateLimitRule

limiter = RateLimiter(RateLimiterConfig(
    rules=[RateLimitRule(10, 60, Algorithm.TOKEN_BUCKET,
                         capacity=20, key_type="user_id",
                         path_pattern="/api/*", name="api-user")],
    fail_open=False,
))
result = limiter.check_request(request)  # framework request with authenticated user
print(result.allowed, result.remaining)
print(limiter.metrics())

# Explicit subjects have no URL, so use an unfiltered rule for direct checks.
result = limiter.check("account-123", RateLimitRule(10, 60))
```

Rules apply case-sensitive `fnmatch` globs to the URL path, excluding its query.
`key_type` supports `ip`, `user_id`, `path`, `method` and `custom` (requires
`key_func`). A custom function takes precedence. Rule names, paths, selectors,
algorithm, window, limit and optional capacity isolate counters. Identical rules
within a request are evaluated once. Give different custom closures distinct
stable names; a function's qualified name cannot identify captured values.

All matching rules are charged sequentially; any denial wins, using the longest
retry delay. There is no transaction spanning multiple rules. `config` returns
a copy. `update_rules()` validates/copies before atomically publishing a whole
rule set; a request keeps its original snapshot while storage I/O proceeds
outside the configuration lock.

## Algorithms

| Algorithm | State and behavior |
|---|---|
| Token bucket | Tokens/refill time; steady rate `limit/window`, independent optional `capacity` |
| Leaking bucket | Virtual occupancy/drain time; only admits a whole request when it fits |
| Fixed window | Counter and window start; boundary bursts remain possible |
| Sliding window log | UUID per **accepted** request; exact rolling accepted quota, bounded by limit |
| Sliding window counter | Weighted current/previous counters; approximation assumes even distribution |

For buckets, `capacity` defaults to `limit` and response `limit` means capacity.
Other algorithms reject `capacity`. Limits/windows/capacity are positive bounded
integers; bucket drain duration is bounded too. The leaking algorithm is an
admission decision and immediately returns; it does not pace HTTP execution.

For actual FIFO pacing use the separate owned worker:

```python
from rate_limiter.queue import LeakyBucketQueue

with LeakyBucketQueue(capacity=10, rate=5) as queue:
    future = queue.submit(lambda: "background result")
    print(future.result(timeout=2))
```

`submit` raises `queue.Full` without blocking when the waiting queue is full.
One worker starts callbacks no faster than `rate`; a slow callback lowers output.
Close cancels waiting work and joins the current callback, which must have a
bounded run time. This local queue has no durable or distributed delivery.

## File publication and metrics

Use [examples/rules.json](examples/rules.json), containing a `rules` array.
Only known fields are allowed, duplicate JSON fields are rejected, and the file
is limited to 64 KiB / 100 rules. Publish complete files with `os.replace`.

```python
from rate_limiter.rule_files import RuleReloader

limiter.reload_rules("rules.json")  # explicit reload; raises on invalid file
with RuleReloader(limiter, "rules.json", interval=1) as reloader:
    serve_application()  # owner stops serving before leaving this scope
```

The optional non-daemon poller retains the last good rules on invalid input.
`reloader.status()` exposes reload/error counts and the last error class.
`limiter.metrics()` returns checks/allowed/denied/storage_errors per **rule
evaluation**, without subject labels; these process-local counters need an
external collector for dashboards and alerts.

## Distributed and HTTP adapters

`RateLimiterConfig(storage_backend="redis", redis_url=...)` shares counters.
Redis uses one EVAL per rule: server TIME and check/update run atomically.
The clock watermark prevents backwards movement while retained. Memory samples
its clock under the storage lock; expiry uses monotonic time. Redis client
clock skew cannot refill quotas. Server time jumps beyond retained state/TTL,
Redis restarts, data loss and administrator edits are outside that guarantee.

`RedisStorage(url, socket_timeout=2, max_connections=64)` supports explicit
timeouts and a bounded connection pool. Automatic command retry is disabled;
a lost response leaves an uncertain quota charge, handled by the configured
fail-open/fail-closed policy. Close owned storage after requests/workers drain.
Only a standalone Redis primary is supported; hash tags do not constitute
Redis Cluster support because sliding counter keys are derived inside Lua.

MemoryStorage defaults to 100,000 keys. Idle expiry sweeps visit at most64 keys
per 50 ms during operations; pressure sweeps visit at most128. Live quota state
is never evicted to admit another identity. Capacity errors follow fail policy;
cleanup can temporarily reject capacity even when other expired keys remain.
Callbacks provide mutual exclusion, not transaction rollback after errors.

Flask: `RateLimitExtension(app, limiter=limiter)`. FastAPI:
`app.add_middleware(RateLimitMiddleware, limiter=limiter)`. See runnable
[Flask](examples/flask_example.py) and [FastAPI](examples/fastapi_example.py) examples.
Async adapters move synchronous storage work to worker threads; cancelling the
request does not forcibly interrupt an already running storage call.

Denials default to429; `default_response_code` supports another 4xx/5xx status.
Both adapters honor `include_headers` and `auto_headers` on allowed and denied
responses. Headers are X-RateLimit-Limit/Remaining/Reset (relative seconds),
plus integer Retry-After and X-RateLimit-Retry-After on denial. The general
decorator returns a response **dictionary** when `raise_exception=False`;
use a framework adapter when an actual HTTP response is required.

`ip_key` uses the connection peer. Opt in to `trust_forwarded=True` only behind
a trusted proxy that strips client forwarding headers. Credential-derived keys
use stable SHA256, not Python's randomized hash; this helper does not validate
JWTs or authenticate API keys. Use an authenticated principal/custom resolver
when the quota is per user rather than per credential.

## Verify real sharing

```sh
python tools/redis_probe.py --url redis://127.0.0.1:6379/0 --output benchmarks/redis-local.json
```

The probe uses a unique namespace, four synchronized Python processes, deliberately
skewed client timestamps and two real local HTTP instances. It deletes only its
own keys and does not flush or stop the supplied Redis. The recorded run used
Redis8.0.5 and an independently owned server that was stopped after verification.

**Migration:** core state keys now use a `v2` namespace with hashed rule/subject
identity, resetting earlier quotas. Coordinate deployment instead of mixing old
and new instances. Proxy defaults, query-free paths and disabled denial headers
also change behavior. See [the review](docs/chapter05-review.md).

Global multi-region quotas, edge routing, Redis HA/Cluster, durable execution
queues and adaptive/ML policy tuning remain deployment/design extensions.
