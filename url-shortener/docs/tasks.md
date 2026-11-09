# Implementation Tasks: Chapter 9 URL Shortener

Status is based on source review on 2026-10-31 and final validation on 2026-11-01. See [requirements](requirements.md), [design](design.md) and the [source comparison](open-source-review_cn.md). This acceptance checklist replaces stale pending statuses and duplicated implementation descriptions.

## Implemented and Verified

- [x] Python >=3.9 src layout, zero runtime dependencies, public models/strategies/storage/errors including SQLiteStorage.
- [x] Immutable URLMapping/ClickRecord,301/302 enum and explicit exceptions.
- [x] In-memory bidirectional index with RLock across lookup/generation/save; no overwrites.
- [x] CRC32 with suffix retries and CollisionLimitExceededError on exhaustion.
- [x] Base62 utilities and injectable ID protocol; at least 7 digits, never truncate larger IDs.
- [x] Shorten, exact-string deduplication, resolution and click analytics; reject invalid input before saving.
- [x] SQLite persistence, unique code/url constraints, transaction recheck and atomic creation.
- [x] Read-only existing-URL fast path while another connection holds a writer reservation.
- [x] Durable one-row ID sequence; allocation joins the same-object mapping transaction and rollback; duplicates consume no IDs.
- [x] Multiple instances/processes, restart, legacy sequential-data initialization, no wrap on exhaustion.
- [x] Bounded instance LRU, 0 disables it; database reads outside the cache lock; cache hits record clicks.
- [x] Shared URL validation, international URI conversion without double-escaping, IPv6.
- [x] Shared WSGI API, 16 KiB body limit, deep JSON 400, missing code 404,302 no-store.
- [x] Demo reuses WSGI with optional SQLite persistence; remove duplicate HTTP protocol/handlers.
- [x] Existing 109 tests plus 36 new regressions, 145 passed.
- [x] Four processes, 1000 shorten calls, 501 unique mappings; resolve all after reopening and allocate a new code.
- [x] Actual HTTP 200/302/400/404; [raw result](../benchmarks/review-2026-11-01.json) and [reproduction tool](../tools/verify_concurrency.py).
- [x] Bilingual README/design/requirements/tasks, learning guide and repository indexes updated.

## Deployment Work Beyond This Implementation

- [ ] Production WSGI, ingress rate limiting, load balancing and capacity planning.
- [ ] Replication/sharding, cross-database IDs, distributed analytics and persistent events.
- [x] Bounded click-detail retention (2026-11-09): exact per-link counters; details capped per link by count (default 1000) and age (default 30 days), amortized O(1) pruning on write.

Local correctness checks do not demonstrate the book's 100-million-new-links/day scale. The Bloom filter is optional and omitted.

## Reproduce

```bash
pip install -e ".[dev]"
python -W error -m pytest tests -q
python tools/verify_concurrency.py --output benchmarks/local.json
python -m examples.demo_server --port 8000 --database urls.db
```
