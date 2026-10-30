# Chapter 12 implementation and verification tasks

Updated 2026-10-29. The [Chinese task list](tasks_cn.md) has matching task IDs. These tasks implement the [requirements](requirements.md); the [chapter audit](chapter12-review.md) records the current review's final verification. Checked items describe the local implementation inspected in this review, not deployment acceptance or a substitute for the final regression run.

## Implemented local reference

| Task | State | Work and requirement trace | Acceptance evidence |
| --- | --- | --- | --- |
| TASK-12-01 | [x] | Compose `NewsFeedAPI`, separate publication/retrieval and expose authenticated POST/GET `/v1/me/feed` (REQ-12-01, REQ-12-14, REQ-12-15). | `api.py`, `http.py`; `test_http.py`, `test_api_lifecycle.py`, `test_chapter12.py`. |
| TASK-12-02 | [x] | Validate and snapshot accepted post payloads, derive identity from tokens and serialize local rate-window updates (REQ-12-02, REQ-12-03). | `auth.py`, `rate_limiter.py`, `post_service.py`; `test_post_service.py`, `test_chapter12.py`, `test_http.py`. |
| TASK-12-03 | [x] | Provide memory/SQLite post and profile adapters, immutable post identity and atomic post/outbox acceptance; replay pending work fairly after restart (REQ-12-03, REQ-12-04). | `storage.py`, `post_cache.py`, `post_service.py`; `test_chapter12.py`, `test_cache_reliability.py`. |
| TASK-12-04 | [x] | Provide graph abstraction, SQLite follow index, cached relationship/count queries and invalidation fencing for concurrent loads (REQ-12-05, REQ-12-15). | `social_graph.py`, `sqlite_graph.py`, `social_graph_cache.py`; `test_social_graph.py`, `test_social_graph_cache.py`, `test_cache_reliability.py`. |
| TASK-12-05 | [x] | Select push/pull by follower threshold, filter recipients and enqueue bounded batches; keep celebrity posts available to indexed reads (REQ-12-06). | `fanout_service.py`, `news_feed_service.py`; `test_fanout_service.py`, `test_chapter12.py`. |
| TASK-12-06 | [x] | Reuse mute/selective-sharing policy across push/read/notification; persist SQLite mute settings and revalidate after asynchronous loads (REQ-12-07). | `policy.py`, `fanout_service.py`, `news_feed_service.py`, `notification_service.py`; `test_mute_policy.py`, `test_notification_reliability.py`, `test_chapter12.py`. |
| TASK-12-07 | [x] | Reserve queue capacity across pending/delayed/claimed states; fence receipts; implement bounded retry/dead letters and cancellation recovery, including supported Python timeout semantics (REQ-12-08). | `queue.py`, `worker.py`; `test_queue.py`, `test_worker.py`, `test_worker_reliability.py`, `test_chapter12_queue_audit.py`. |
| TASK-12-08 | [x] | Maintain bounded, sorted and deduplicated per-reader ID caches; make partial worker retry safe (REQ-12-08, REQ-12-09). | `news_feed_cache.py`; `test_news_feed_cache.py`, `test_worker_reliability.py`, `test_chapter12.py`. |
| TASK-12-09 | [x] | Merge hybrid candidates and implement validated timestamp/ID keyset pagination; reuse fixed workers for cached-ID loading, classification and author pulls, preserving order during concurrent hydration (REQ-12-10, REQ-12-15). | `news_feed_service.py`; `test_chapter12.py`, `test_http.py`, `test_chapter12_read_audit.py`. |
| TASK-12-10 | [x] | Repair cold/expired/changed/historical windows from source; scan beyond hidden batches and retain a non-sliding repair deadline (REQ-12-11). | `news_feed_service.py`, `storage.py`; `test_chapter12.py`, `test_cache_reliability.py`. |
| TASK-12-11 | [x] | Wire all five cache groups into hydrated reads; keep content/profile source fallback and mutable-value isolation; coalesce same-author profile misses behind bounded striped locks (REQ-12-12). | Content, graph, ID, action, counter and profile cache modules; `test_action_cache.py`, `test_counter_cache.py`, `test_user_cache.py`, `test_cache_reliability.py`, `test_chapter12.py`, `test_chapter12_read_audit.py`. |
| TASK-12-12 | [x] | Record per-recipient bounded deduplicated local new-content notifications with fixed recipient-validation workers (REQ-12-13). | `notification_service.py`; `test_notification_service.py`, `test_notification_reliability.py`. This is local simulation, not third-party push. |
| TASK-12-13 | [x] | Own one event loop and shared start/stop, drain admitted publications and recover cancelled claims; bound HTTP concurrency/size/deadlines and offload blocking storage. Observe publish/retrieve outcomes, acceptance/read latency and bucketed recent QPS through `api.metrics()` (REQ-12-14, REQ-12-15, REQ-12-16). | `api.py`, `concurrency.py`, `http.py`, `worker.py`, `metrics.py`; `test_api_lifecycle.py`, `test_worker_reliability.py`, `test_http.py`, `test_chapter12.py`, `test_chapter12_metrics.py`. |

## Review and documentation gates

| Task | State | Work | Completion criterion |
| --- | --- | --- | --- |
| TASK-12-14 | [x] | Re-read chapter 12 and use CodeGraph/code-review-graph to trace the publication, fanout, read, cache and test paths. | Source-backed requirement matrix; graph results are navigation evidence, not proof that dynamic calls or tests are complete. See the chapter audit. |
| TASK-12-15 | [x] | Restore English/Chinese `requirements` and `tasks`, with stable IDs, acceptance mapping and explicit local/deployment boundaries. | All four documents exist, cross-links resolve, requirement/task IDs agree across languages; README/design/audit are updated by the coordinating review. |
| TASK-12-16 | [x] | Complete the current review's final regression and static/documentation checks. | 2026-10-29: 282 passed, no failures/skips, with warnings as errors. Source/examples/new tests pass format and Python3.9 syntax checks; chapter-wide Ruff E9/F and documentation links pass. See the chapter audit for runtime and local-loopback limitations. |

## Deployment and operations backlog

These are intentionally open; they cannot be checked merely because the local adapter has a similar interface.

| Task | State | Requirement | Acceptance criterion |
| --- | --- | --- | --- |
| TASK-12-17 | [ ] | Production monitoring integration (REQ-12-16). | Integrate the local request snapshot and queue diagnostics with an exporter, latency distributions, aggregation, dashboards and alerts. Add queue age monitoring and verify alert conditions. Distinguish service metrics from likes/replies; the local request snapshot itself is complete under TASK-12-13. |
| TASK-12-18 | [ ] | Media/CDN and real notification providers (REQ-12-17). | Configure upload/origin/CDN and provider adapters; verify owned integration endpoints, retries, idempotency/receipts and recovery behavior. |
| TASK-12-19 | [ ] | Shared cache, durable broker and global quotas (REQ-12-18). | Define cross-instance contracts, durable job completion/leases, global capacity and quota semantics; test process loss between dispatch and acknowledgement. |
| TASK-12-20 | [ ] | Sharding, replication/read replicas and multiple data centers (REQ-12-18). | Define routing/migration, consistency and recovery objectives, then exercise failover and stale-read behavior under failures. |
| TASK-12-21 | [ ] | Capacity validation (REQ-12-19). | Publish a reproducible workload and environment, error counts and latency percentiles before claiming the book's scale or a production SLO. |

## Running acceptance checks

From `news-feed-system`, run `python -m pytest tests -q -p no:cacheprovider`. On restricted Windows setups, point `TEMP`/`TMP` at an existing writable directory under the repository's `.test-tmp`. Tests are isolated from other chapter suites; external push is not sent.

Install the package with `python -m pip install -e ".[dev]"` first, or set `PYTHONPATH` to this project's absolute `src` directory so subprocess recovery tests can import it. The Windows event loop and HTTP tests require local loopback sockets.

For source checks, follow the root [contribution guide](../../CONTRIBUTING.md). Record final outcomes in [chapter12-review.md](chapter12-review.md), then update TASK-12-16 in both languages. A completed local queue test does not close the durable-broker task; `queue.join()` does not include an undispatched outbox row.
