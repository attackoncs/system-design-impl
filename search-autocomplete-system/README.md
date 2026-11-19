# Search Autocomplete System

<!-- LEARNING_GUIDE_START -->
## Code reading guide

[Learn through a concrete example: architecture, state changes and annotated source (Chinese)](docs/learning-guide_cn.md).
<!-- LEARNING_GUIDE_END -->

Reviewed against Chapter 14, fixed GitHub source and CodeGraph on 2026-11-18. This review fixes database identity across cwd changes, SQLite ID overflow and trickled request headers. See the [source comparison](docs/open-source-review_cn.md) and [verification record](benchmarks/verification-2026-11-18.json).

Chapter 14 reference implementation: completed-search logs, offline aggregation,
atomic snapshots, cached top-five tries, filtering, balanced shards and HTTP replicas.
Includes stable analytics sampling and an interruptible periodic snapshot worker.
Standard library runtime, Python 3.9+. [中文说明](README_CN.md) · [Design](docs/design.md).

## Run

```bash
pip install -e ".[dev]"
pytest -q
python examples/basic.py
python examples/network_demo.py
```

The network demo owns three shard processes and one coordinator, kills one replica,
verifies the same global ranking, then verifies immediate removal. All temporary
processes/data are cleaned up. It sends no analytics to external services.

```bash
python -m search_autocomplete.cli --db demo.sqlite3 record event-1 "twitch" --at 100
python -m search_autocomplete.cli --db demo.sqlite3 build --start 0 --end 200 --shards 2
python -m search_autocomplete.cli --db demo.sqlite3 query tw
python -m search_autocomplete.cli --db demo.sqlite3 block twitch
python -m search_autocomplete.cli --db demo.sqlite3 unblock twitch
python -m search_autocomplete.cli --db demo.sqlite3 aggregate --start 0 --end 200
python -m search_autocomplete.cli --db demo.sqlite3 aggregation 1
python -m search_autocomplete.cli --db demo.sqlite3 worker --once --shards 2
# Long-running worker: build immediately, then every seven days. Ctrl-C stops it.
python -m search_autocomplete.cli --db demo.sqlite3 worker --shards 2
```

Build without time arguments uses the preceding seven days. The worker defaults
to the same trailing window and a seven-day interval; scheduling state is local
to the worker process, while generations and materialized counts are durable.
For calendar weeks, pass explicit UTC week boundaries to `aggregate`/`build`.
Transient SQLite/publication failures retry after five seconds, preserving the
previous active generation. Analytics events are completed searches,
not every suggestion request. Explicit event timestamps use UTC epoch seconds.
Retry an event with the same ID/query/time; omitted timestamps reuse the first event's
time. Conflicting reuse is rejected. Windows examples also accept PowerShell commands.

Use `record ... --sample-every 10` or `Collector(store, sample_every=10)` to log
approximately one event in ten. Selection hashes event IDs, so retries and separate
processes agree; keep the factor fixed for one stream. Counts are sampled counts,
not extrapolated totals. Ordinary `Store.record` remains unsampled.

After blocking a query, `purge-blocked` deletes its source events and obsolete
aggregations. `worker --purge-blocked` performs that cleanup before each build.
This is an explicit maintenance operation: unblocking cannot recover deleted
source events. Hashed query receipts prevent retried event IDs from restoring them.
Historical serving snapshots remain immutable and need a separate retention policy;
unblocking can expose a term in an explicitly selected historical snapshot.

## HTTP shards

Publish at least two distinct queries before requesting two shards. Actual shard
count is bounded by distinct query count. Run each node in a separate terminal:

```bash
python -m search_autocomplete.cli --db demo.sqlite3 node --shard 0 --port 8100
python -m search_autocomplete.cli --db demo.sqlite3 node --shard 0 --port 8101
python -m search_autocomplete.cli --db demo.sqlite3 node --shard 1 --port 8102
```

Create `replicas.json` using the current snapshot's shard IDs:

```json
{"0":["http://127.0.0.1:8100","http://127.0.0.1:8101"],"1":["http://127.0.0.1:8102"]}
```

```bash
python -m search_autocomplete.cli --db demo.sqlite3 coordinator --replicas replicas.json --port 8080
```

GET `http://127.0.0.1:8080/suggest?prefix=tw`. Responses include version, filter
revision and query/frequency suggestions. Every relevant shard must be available;
missing shards or incompatible versions return 503 rather than partial rankings.
Invalid input returns 400. Prefixes are normalized English letters/spaces, at most
50 characters; trailing prefix spaces remain significant. Empty prefixes return no
suggestions after a snapshot is published. Scores descend, lexical order breaks ties.

Blocking affects the next request, including cached responses. Requests recheck
the rule revision before returning; a concurrent change can produce 503 for retry.
Later builds exclude blocked terms. Without source cleanup, unblocking restores a
term in the next build; existing historical generations retain their original data.
Responses use `Cache-Control: no-store` so browser caches cannot bypass removals.
This intentionally omits the chapter's one-hour browser cache optimization.

## Validation and limits

File paths bind to an absolute location when a Store is constructed. Changing cwd
cannot redirect offline readers; empty paths are rejected, while the default
shared-memory mode remains supported. Durable snapshot/aggregation IDs must fit
positive SQLite int64 values: overflow returns HTTP 400, missing in-range versions
return 503. Inbound request lines and headers share an absolute three-second budget;
the timer is cancelled and joined before service execution. This does not impose
a new hard execution deadline on SQLite or custom service code.

The regression suite covers actual HTTP merge/fallback and independent-process
termination, cross-instance event deduplication, nonblocking builds, publication
races, sampled collection, worker retries, immutable tries and ranked filtering.
**196 tests pass**, including the unchanged 188 existing cases and eight focused
regressions. Source coverage is 93.37% statements / 87.59% branches, with no excluded
lines. See the [chapter audit](docs/chapter14-review.md). Existing test and benchmark
source was preserved. Both original examples, including process failover/removal,
passed. The [latest report](benchmarks/results.md) records the original tool's
10,000 queries, 10,000 local lookups and 500 real HTTP requests. It is a sequential
local observation, without a paired baseline or production-capacity claim.

SQLite log/snapshot/metadata storage is shared on one host. The query protocol
supports separate replica processes, but this is not multi-host storage HA or a
consensus metadata service. Keep replicas on the same published snapshot lineage;
generation IDs are local to that database. Normal top-five lookup follows only the
prefix. Filtered refill uses ranking bounds to prune branches; excluding many terms
can still visit the whole matching subtree. Cache loads/builds happen outside the
global LRU lock. Outstanding coordinator tasks are bounded by `concurrency`, with a
total `request_timeout` across admission and failover. SQLite/Store metadata waits
and OS DNS resolution can outlive that deadline; active network tasks retain their
bounded slots until they exit. Caches/HTTP handlers are bounded;
historical events, snapshots, aggregates and deduplication receipts need an operator
retention policy. Scaling targets of 10 million daily users / 48,000 peak QPS are
not established by the local sequential benchmark. Live trending, Unicode/country
ranking, authentication and TLS deployment are extension points.
