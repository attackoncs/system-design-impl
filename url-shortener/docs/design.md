# Design: URL Shortener

The central invariant is an immutable, bidirectionally unique mapping. A strategy proposes a code; storage atomically accepts or reuses the mapping. HTTP adapts these operations. See the [illustrated learning guide](learning-guide_cn.md) and [book/open-source review](open-source-review_cn.md). Source links below replace obsolete copies of the implementation.

## Components and Dependencies

| Component | Responsibility | Maintained source |
|---|---|---|
| URLShortener | Validation, shortening, resolution, LRU, click records | [shortener.py](../src/url_shortener/shortener.py) |
| HashCollisionStrategy / Base62Strategy | CRC32 retries or full unique-ID encoding | [strategies.py](../src/url_shortener/strategies.py) |
| StorageBackend / InMemoryStorage | Storage contract; dual dictionaries under RLock | [storage.py](../src/url_shortener/storage.py) |
| SQLiteStorage | Persistent unique mappings, transaction reuse, durable IDs | [sqlite_storage.py](../src/url_shortener/sqlite_storage.py) |
| Models | Immutable URLMapping/ClickRecord and RedirectType enum | [models.py](../src/url_shortener/models.py) |
| redirect_uri | HTTP(S) validation and ASCII URI conversion | [urls.py](../src/url_shortener/urls.py) |
| create_app | Bounded JSON input, redirects, error statuses | [http.py](../src/url_shortener/http.py) |
| Demo | Reuses the WSGI adapter | [demo_server.py](../examples/demo_server.py) |

Strategies depend on storage abstractions, not HTTP. SQLite implements the existing `IDGenerator.next_id()` protocol without type-specific branching in the orchestrator. Python >=3.9; no runtime dependencies.

## Creation and Transaction Ownership

1. `shorten` validates the URL and 301/302 configuration before persistence.
2. SQLite first performs a read-only long-URL lookup. Immutable mappings can return immediately without a writer reservation.
3. On a miss, acquire `BEGIN IMMEDIATE` and recheck: another instance may have created the mapping in the meantime.
4. Only then run the factory, generate a candidate and INSERT. Unique code/url constraints prevent overwriting. Commit on success; roll back on exceptions.
5. The factory and ID allocator reuse the thread-local connection. Close connections at operation boundaries; do not share sqlite3 connections across threads.

InMemoryStorage uses one RLock around lookup/generation/save. Custom backends must implement an atomic `get_or_create`; the base implementation is a sequential fallback.

## Durable IDs and Encoding

```python
storage = SQLiteStorage("urls.db")
service = URLShortener(storage=storage, strategy=Base62Strategy(id_generator=storage))
```

Use the **same storage object** for allocation and persistence inside an instance, so allocation joins the mapping transaction. Separate processes may use separate objects sharing one SQLite file. The one-row `id_sequence` increments transactionally. Duplicate URLs consume no IDs; failed mapping creation rolls allocation back. Standalone `next_id()` commits independently, so gaps are permitted.

On first initialization, seed from the maximum existing `urls.id`. This supports legacy local Base62 data written sequentially from 1, not arbitrary external IDs. Existing external-ID or mixed-strategy databases need coordinated sequence migration. The maximum SQLite ID is `2**63-1`; exhaustion raises OverflowError without wrapping.

Base62 uses `0-9a-zA-Z`, left-padding to at least 7 characters without truncation. `62**7` needs 8 digits. Uniqueness depends on coordinated IDs: the default AutoIncrementIDGenerator is thread-safe within one process and does not survive restart.

The default hash strategy uses 32-bit CRC32 and suffix retries, at most 10 retries by default. Padding to 7 characters adds no entropy. Neither a 3.5-trillion hash capacity nor success for every URL within a bounded retry budget is claimed.

## Resolution, Cache and Analytics

Look up and update LRU order under the cache lock. Read storage outside that lock, then briefly reacquire it to fill/evict. Default capacity is 1024;0 disables caching. Mappings cannot be changed or deleted, so no invalidation protocol is necessary.

Unknown codes raise ShortCodeNotFoundError. Successful resolutions, including cache hits, update analytics under a separate lock: an exact per-link counter is incremented, and one ClickRecord is appended to a per-link `deque(maxlen=click_retention_max_records)` (default 1000; 0 keeps no details). Timestamps are taken under the lock, so each deque is time-ordered and records older than `click_retention_max_age` (default 30 days) are popped from the left on write and before reads, amortized O(1) per click with no scans. `get_click_records` returns a copy of the retained window; `get_click_count` stays exact. Memory is O(links + links x max records). Details are nonpersistent instance memory. High-volume analytics need an external persistent event/aggregation system.

## URL Identity and HTTP

Storage and library resolution preserve the input string; deduplication uses exact string equality. HTTP Location converts international hostnames with IDNA and other Unicode with UTF-8 percent encoding, preserving existing escapes. Validation performs no network fetch. Reject missing hosts, invalid ports, whitespace, controls, backslashes and unencodable Unicode.

Preserve IPvFuture brackets and explicit empty `?` / `#`. Reject lossy stdlib IDNA2003 Unicode conversions using an NFC round-trip check; require explicit ASCII ACE instead. This does not implement full IDNA2008. See [standards and boundaries](open-source-review_cn.md).

| Request or condition | Result |
|---|---|
| POST `/api/v1/data/shorten`, JSON `longUrl` | 200 and JSON `shortURL` |
| GET `/<code>` or `/api/v1/<code>` | Stored 301/302 with ASCII Location |
| 302 | `Cache-Control: no-store` |
| Unknown code/path | 404 |
| Invalid URL/JSON/body length/deep JSON | 400; body bound 1..16384 bytes |
| Invalid redirect configuration | Library ValueError before saving |
| Immutable mapping conflict | Library ValueError; never overwrite |

Browsers may cache 301 redirects; click counts cover only requests that reach `resolve`. No HTTP analytics API is supplied. The previous demo-only `/shorten` and `/stats` protocol has been replaced by the shared chapter API.

## Deployment Boundaries

SQLite remains a local single-writer database. No sharding, replication, cross-database ID allocation or shared cache is supplied. Use a production WSGI server for concurrent HTTP; the stdlib demo is single-threaded. The chapter 5 rate limiter can be integrated ahead of HTTP. Load balancing, analytics and database replication remain deployment extensions. The optional Bloom filter is omitted; uniqueness is enforced by storage.

## Verification

- [Existing chapter tests](../tests/test_chapter_review.py): shared-file deduplication, restart, immutable conflicts and cache.
- [New regressions](../tests/test_open_source_review.py): durable IDs, rollback, exhaustion, legacy initialization, read-only duplicate requests during a write transaction, HTTP input and international URLs.
- [Properties](../tests/test_properties.py): format, round-trip, idempotence and click accuracy; large IDs additionally have a no-truncation regression.
- [Multiprocess/HTTP verification](../tools/verify_concurrency.py) and [observations](../benchmarks/REPORT.md).
