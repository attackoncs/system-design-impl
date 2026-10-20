# Metrics verification report

2026-10-20, Asia/Shanghai. Python 3.13.9 / Windows 11 build 26100. Standard
library only. Network tests/demo used approved unrestricted localhost processes
because the restricted Windows sandbox blocks loopback networking. No external
email, phone, PagerDuty, cloud account, Kafka or commercial TSDB was contacted.

## Tests

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

## Demo evidence

`python examples/network_demo.py` completed with 3 brokers / 1 coordinator /
3 TCP proxies / 6 monitoring processes. Actual pull metric python_memory_bytes
arrived; **530.820ms** from push before HTTP request to query visibility for the
demo series. Webhook attempts **2**, unique notifications **1**, dedupe after
lost confirmation true. Processes are stopped on exit; diagnostic/runtime files
remain ignored. This is localhost latency, not an alert delivery SLA.

## Query/cardinality/storage experiment

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

## Explicit production gaps

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
