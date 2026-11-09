# Design: Web Crawler

The core loop claims a URL and its host, checks robots, downloads and discovers links, acknowledges the outcome, then returns discoveries to scheduling. See the [illustrated learning guide](learning-guide_cn.md) and [book/source review](open-source-review_cn.md). Maintained-source links below replace stale copies of the implementation.

## Components and State Ownership

| Component | Responsibility | Source |
|---|---|---|
| Crawler | Batch attempts/results, bounded tasks, content claims and cancellation | [crawler.py](../src/web_crawler/crawler.py) |
| URLFrontier | Per-host priorities, ready heads, delayed hosts and reservations | [frontier.py](../src/web_crawler/frontier.py) |
| SQLiteCrawlState | jobs/hosts/fingerprints/snapshots, ownership and recovery | [state.py](../src/web_crawler/state.py) |
| URLLibFetcher / DNSCache | Bounded downloads/deadlines, TTL/LRU address cache | [fetcher.py](../src/web_crawler/fetcher.py), [dns.py](../src/web_crawler/dns.py) |
| RobotsChecker | Origin cache, miss coalescing, rules and Crawl-delay | [robots.py](../src/web_crawler/robots.py) |
| ContentParser / LinkExtractor | HTML identification, first base href and per-link isolation | [parser.py](../src/web_crawler/parser.py) |
| Filters and deduplication | Admission, normalized URL identity, SHA-256 content identity | [filters.py](../src/web_crawler/filters.py), [dedup.py](../src/web_crawler/dedup.py) |
| ConsistentHashPartition | Host owner and explicit routing contract | [partition.py](../src/web_crawler/partition.py) |
| Config and models | Typed settings, priorities and results | [config.py](../src/web_crawler/config.py), [models.py](../src/web_crawler/models.py) |

Python>=3.9, zero runtime dependencies. Fetchers are replaceable and content hooks support sync/async callables. Default scheduling is in-memory; state_path enables the durable path.

## Scheduling Eligible Hosts

Each host owns a heap ordered by priority and global enqueue sequence. Eligible heads enter the ready heap; delayed hosts enter a wake-up heap. A new higher-priority URL may supersede a published head, whose old entry is invalidated lazily by sequence number. Equal priorities are FIFO; HIGH precedes MEDIUM then LOW.

`get(reserve=True)` holds the host until `complete(entry)`. The next delay starts on completion; first requests have no previous access to wait for. A reserved-host backlog is not rescanned per poll. Heap work is amortized logarithmic per entry, but one get can wake multiple hosts or discard stale entries. Synchronous frontier methods belong to the event-loop thread.

The crawler bounds task creation before semaphore-protected processing. max_pages bounds attempts plus inflight tasks, including errors, duplicates and disallowed pages. When capacity and budget remain, recheck due hosts every 50 ms; a full batch waits for completion. A slow host cannot strand an idle slot, while same-host tasks remain serialized.

## Durable Acceptance and Recovery

```python
crawler = Crawler(CrawlerConfig(
    state_path="crawl.db", max_pages=100, max_concurrent=8,
    per_host_delay=1, recrawl_interval_seconds=3600,
))
results = await crawler.crawl(["https://example.org/"])
# A later external scheduling cycle:
results = await crawler.crawl([])
```

One state file has one active owner, enforced by an OS file lock released on process death. Claims become inflight; startup recovers unacknowledged jobs. HTML snapshots, fingerprints, acknowledgements and host readiness commit together. Errors retry within a durable budget; cancellation returns unfinished work. Blocking database I/O runs in threads and started operations drain before releasing the lock/connection. `_drain` shields accepted database work and child-task cleanup against repeated cancellation; cancellation is re-raised after completion. Nested finally blocks close the owner and reset `_running` even when cancellation arrives during shutdown, so late open threads cannot leak locks and a cleaned Crawler remains reusable.

Only a bounded eligible-host window is loaded into memory. Dispatch walks distinct pending hosts through the `host_jobs(state,host,priority)` index and takes each eligible host's due head with `LIMIT 1`, so a claim costs O(hosts·log n) rather than ranking every pending row. There is no extra periodic write-behind buffer. Each call returns its batch results and preserves pending work. Due recrawls activate on batch startup. Unchanged pages back off to at most 16 times the base interval; changed content restores the base interval and raises priority.

## Robots and URL Boundaries

Cache by origin and coalesce misses with striped locks. A small RFC 9309 parser replaces urllib.robotparser: consecutive user-agent lines share a group, blank lines do not end groups, every group naming the crawler's product token (case-insensitive exact match, version stripped) is merged, and merged `*` groups apply only when no specific group exists. Matching supports wildcard *, terminal $, longest-rule precedence and equal-length Allow precedence; /robots.txt itself is always allowed. Parse URLs before normalizing escaped octets: %23 stays in the path, %2F stays escaped, and literal %2A/%24 do not become operators. Decode unreserved escapes and compare Unicode through UTF-8 encoding. Empty paths with queries begin at /.

Wildcard matching uses ordered substring searches rather than a backtracking regex. Percent conversion uses fixed-length matches. Fetch policy: 2xx bodies are parsed up to 500 KiB; up to five redirects are followed (even cross-origin) and more, a 3xx without Location, or any 4xx mean no restrictions; 5xx means complete disallow. Network failures (FetchError without status) allow by default, the documented compatibility choice; `robots_unreachable_policy="disallow"` applies the RFC policy. Failures are retried after at most 60 s and the last successful result is reused meanwhile; cache TTL is capped at 24 h. Redirect targets re-enter admission, robots and scheduling by default; see [transport limits](transport-review.md) for opt-in automatic same-origin following.

Use the first base href, falling back to the document URL when missing/malformed. Skip a malformed href individually. Normalize scheme/host and remove fragments while preserving path, query and userinfo case. Filter malformed hosts/ports and non-HTTP(S) before enqueue; seeds and discoveries share admission.

## URL Identity and Content Identity

URL deduplication prevents repeated admission of one normalized address. Fingerprints prevent repeated archiving of equal content at different addresses. One task owns a transient claim; others wait for its acceptance and retry after owner failure/cancellation.

Equal HTML in different directories can reveal different relative URLs. DUPLICATE pages still discover/enqueue links while omitting repeated snapshots/hooks. Hook errors are isolated; external side effects do not share the SQLite transaction. A crash may replay a successful hook, so consumers need URL/fingerprint idempotence.

## Distribution and Deployment Limits

Host partitions select owners. Persist remote discoveries before route_url delivery; acknowledge only after durable target acceptance and support duplicate delivery. Workers use separate state files and external safe handoff on membership changes; see the [partition contract](partition-review.md).

No membership service, message broker, automatic safe migration, database replication/sharding, five-year retention cleanup, anti-spam model or rendering cluster is supplied. Strict priority is not probabilistic/PageRank scheduling and may starve low priorities. System getaddrinfo cannot safely be interrupted and retains its bounded slot until return. Application DNS TTL is not authoritative TTL. No connection pool, HTTP2 or production capacity guarantee is supplied.

## Verification

- [New regressions](../tests/test_open_source_review.py): complexity, randomized scheduling oracle, idle capacity, robots encoding and link parsing.
- [Durability](../tests/test_durable_crawl.py), [lifecycle](../tests/test_lifecycle.py), [routing](../tests/test_partition_pipeline.py), [real transport](../tests/test_transport_review.py).
- [Repeated-cancellation regressions](../tests/test_cancellation_review.py): controlled open/finish/close threads, ownership retained until completion, cancellation propagation and Crawler reuse.
- [Reproducible scheduler and HTTP checks](../benchmarks/REPORT.md): 259 tests, historical identical-workload comparison and current owned loopback/resumption checks.
