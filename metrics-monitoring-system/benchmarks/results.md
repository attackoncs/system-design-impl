# Metrics verification report

## Core review — 2026-10-26 (Asia/Shanghai)

Python 3.13.9 / Windows 11 build 26100, standard library only. Real localhost
fixtures used approved unrestricted processes because the Windows sandbox blocks
their sockets. Owned processes were stopped and temporary paths isolated in the
workspace; diagnostic private configs/disks/logs remain ignored under `.runtime/`.
No commercial provider, Kafka deployment, cloud account or professional TSDB was used.

### Final regression

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -q
```

**108 tests passed in 26.333s, no skips and no ResourceWarning.** The pre-change
baseline passed 15 tests in 20.412s. Final coverage contains:

| Area | Tests |
| --- | ---: |
| Original storage, alerts/collection and real process fixtures | 15 |
| Storage/query/cache/rollup/archive reliability | 23 |
| Alert configuration, evaluation and delivery reliability | 22 |
| Collector, partition transport and discovery reliability | 18 |
| HTTP, direct transport and protocol/overload ownership | 20 |
| CLI, rule loading and worker lifecycle | 10 |

New actual SQLite/file behavior covers input ownership before writer waits,
clock validation after acquisition, isolated/coalesced cache results, aggregation
outside the writer and revision retries, dirty-only recomputation, retained-tier
seams, independent counter-reset oracles and gauge sample-weighted grouping.
Archive creation/read budgets, identity/checksum/schema checks, duplicates and
failed fsync cleanup are exercised separately from production power loss.

Pipeline tests use clearly labeled controlled transports/clocks to verify
partition-FIFO fenced claims, spool capacity, unknown-result retry, multiple queue
assignments, target-ring ownership and failed target reload retention. Actual
network fixtures remain independent process/socket tests. Alert regressions cover
immutable/atomic imports, no-data, snapshot publication, lease attempts and stable
delivery IDs; static severity labels do not modify selectors. Protocol checks
exercise real sockets, bounded admission/deadlines and shutdown drain. Injected
providers/failures are not presented as real commercial delivery.

### Independent-process demonstrations

```powershell
python examples/network_demo.py
python examples/chapter_demo.py --output benchmarks/features-2026-10-26.json
```

The original fixture still uses thirteen processes and actual chapter 20 brokers,
collector/exporter/query/storage/alerts and an owned webhook receiver. Its current
push-to-query observation was **559.953ms**; the receiver saw **two attempts and
one unique notification** after a lost-confirmation retry. This is localhost
evidence, not an end-to-end delivery SLA or external-provider validation.

The [chapter feature report](features-2026-10-26.json) records **14 independent
processes**: three broker disks, coordinator, three TCP proxies, two collectors,
exporter, API, storage worker, alert worker and owned webhook. Four scrape targets
were each owned once across the two static collectors, all three queue partitions
were active, gauge grouping returned **40.0 with count 3**, push replay returned
the identical response, and the webhook carried **severity=page**.

### Current query experiment

```powershell
python tools/benchmark.py --output benchmarks/review-2026-10-26.json
```

[Raw current report](review-2026-10-26.json):

| Metric | Observed |
| --- | ---: |
| Gauge source series / raw samples | 100 / 6000 |
| Direct fixture ingestion | 0.806s |
| Query requests / clients | 40 / 4 |
| Points returned per query | 3000 |
| Cold query | 228.463ms |
| Warm p50 / p95 | 65.693 / 90.315ms |
| Warm query QPS | 56.76 |
| SQLite including sidecars | 3,276,800 bytes |
| Request errors | 0 |

This measurement overlapped the chapter 22 baseline's eleven tests, so host
contention affected comparability. It is not a controlled before/after speedup
measurement and does not establish production capacity. Ingestion calls Store
directly, not the queue. Queries repeat one fixed range/selector and warm the
revision cache; serialization and real loopback HTTP are included. p95 is sorted
index floor(0.95×(n−1)), only forty samples. The historical report/raw JSON below
keeps its original date and must not be substituted for current evidence.

### Graph and production limits

code-review-graph's final incremental refresh updated 24 files without build
errors. Explicit impact on chapter 21's six existing core files reported 60 direct
changed nodes, 33 impacted nodes, six additional files and 761 unresolved calls;
edge output was truncated. The new untracked collector.py file_summary explicitly
returned target not indexed. Dynamic calls/new modules need source and execution
cross-checks; empty graph edges are not proof of no callers/tests. See
[chapter mapping](../docs/chapter21-review.md).

SQLite remains a bounded reference engine rather than the book's professional
distributed TSDB. Static ring membership must match across collectors; there is
no consensus service or dynamic failover. Metrics use chapter 20's real queue,
while alerts use a local durable delivery table rather than a Kafka event bus.
Only the owned localhost webhook was delivered. Compression/sharding/replication,
metadata HA, priority streaming, full PromQL/Grafana, TLS/ACL, commercial providers,
cloud tiering, full metadata GC, disaster recovery and sustained ten-million-series
or power-cut validation remain extensions. Socket budgets cannot interrupt
accepted DNS/SQLite/CPU work. No commit or push was made.

## Historical verification — 2026-10-20

2026-10-20, Asia/Shanghai. Python 3.13.9 / Windows 11 build 26100. Standard
library only. Network tests/demo used approved unrestricted localhost processes
because the restricted Windows sandbox blocks loopback networking. No external
email, phone, PagerDuty, cloud account, Kafka or commercial TSDB was contacted.

### Tests

`python -m unittest discover -s tests -v`: **15 passed in 22.383s**.

Seven storage tests exercise actual SQLite files: durable batch replay, immutable
sample conflicts and rollback, type/cardinality budgets, out-of-order insertion,
label filtering/cache revisions, weighted gauge mean, counter resets/composition
across minute/hour summaries, live-tail/tier seams and verified local gzip archive
after online retention expiry. Fake clock advancement covers 8/31/366 days; it is
not an actual year-long soak.

Six alert/pipeline unit tests cover duration/flapping, missing/stale data policy,
grouping, durable frozen retry keys and reopen, silence/superseded transitions,
unconfigured-provider dead letters, collector backlog/FIFO retry and a clock that
advances on every call. Recording providers and injected outages in those tests
are explicitly simulated unit faults.

Two real network tests use thirteen independent processes per full fixture:
three chapter 20 brokers with separate disks, its single coordinator and three
transparent TCP proxies, then exporter, collector, query API, storage worker,
alert worker and an owned localhost webhook receiver. API/worker/alert roles share
one local TSDB file, not a distributed SQL cluster.

- Actual process CPU seconds, Python allocation memory and request counters flow
  through discovery/scrape → collector outbox → quorum queue → TSDB. Push auth and
  replay IDs are checked. Stop storage, enqueue data, verify no premature query
  visibility, restart and recover exactly one stored sample. Stop exporter and
  observe collector_up=0. HTTP SVG parses as XML; dashboard HTML is served.
- Fire two matching series grouped by instance. The receiver commits its dedupe
  record, then deliberately returns HTTP 500 (lost-confirmation fault injection).
  Retrying uses the same notification ID; receiver sees one unique notification
  and at least two attempts. Cool samples produce a grouped resolved notification.

The first real-clock run exposed a bug: treating the current time as an absent
silence deadline continually postponed delivery. An explicit zero/no-silence
sentinel and refreshed claim time fixed it; an advancing-clock unit test prevents
the fixed-clock test suite from hiding it again. No claim of browser screenshot
QA or external provider integration is made.

### Demo evidence

`python examples/network_demo.py` completed with 3 brokers / 1 coordinator /
3 TCP proxies / 6 monitoring processes. Actual pull metric python_memory_bytes
arrived; **530.820ms** from push before HTTP request to query visibility for the
demo series. Webhook attempts **2**, unique notifications **1**, dedupe after
lost confirmation true. Processes are stopped on exit; diagnostic/runtime files
remain ignored. This is localhost latency, not an alert delivery SLA.

### Query/cardinality/storage experiment

`python tools/benchmark.py --output benchmarks/query-results.json`

| Metric | Observed |
| --- | ---: |
| Gauge series / raw samples | 100 / 6000 |
| Fixture ingestion time | 0.622s |
| Independent query process / clients | 1 / 4 |
| Warm HTTP requests | 40 |
| Points returned per query | 3100 |
| First query including compaction/cache fill | 137.077ms |
| Warm p50 / p95 | 36.924 / 67.171ms |
| Warm queries per second | 100.29 |
| SQLite including sidecars | 2,498,560 bytes |
| Request errors | 0 |

Fixture ingestion calls Store directly and is **not queue ingestion throughput**.
Queries use a fixed time range/selector so the revision cache warms. Response
serialization, loopback HTTP and clients are included; the cold query additionally
builds eligible rollups. p95 uses sorted index floor(0.95×(n−1)), only 40 samples.
Tests/demo were also finishing during measurement, so host contention can vary
results. No sustained load, ten-million-series budget, compression ratio or
production capacity is established. Raw data: [query-results.json](query-results.json).

### Explicit production gaps

No TSDB sharding/replication, consensus metadata, queue coordinator HA, WAL/object
archive service, production TLS/ACLs, commercial email/phone/PagerDuty provider or
cloud deployment was used. Storage compaction recomputes retained summaries and
query/evaluation budgets are deliberately small. Receipts/alert metadata need
broader GC/quotas and storage files need backup/disk lifecycle. The chapter 20
queue's 8MiB partition cap, retention and SPOF still apply; offset expiry requires
operator recovery. Counter queries are reset-aware bucket statistics, not PromQL
extrapolation. Coarse partial ranges disclose whole-bucket overfetch. Local archives
have independent user-managed retention. Notifications are at least once and need
receiver idempotency for duplicate-free effects.
