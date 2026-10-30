# Metrics Monitoring and Alerting (chapter 21)

<!-- LEARNING_GUIDE_START -->
## Code reading guide

[Learn through a concrete example: architecture, state changes and annotated source (Chinese)](docs/learning-guide_cn.md).
<!-- LEARNING_GUIDE_END -->

Durable push/pull infrastructure metrics, real queue transport, indexed time
series, reset-aware rollups, stateful alerts and retry-safe webhooks.
[中文](README_CN.md). Logs/traces and business analytics are excluded.

Core review: 2026-10-26. This round adds partition-aware collection and dispatch,
bounded concurrent scraping, cross-series gauge queries, incremental rollups and
atomic rule-file import. Final execution evidence is in [validation](benchmarks/results.md)
and the [chapter mapping](docs/chapter21-review.md); it does not establish production scale.

## Run the reference

Python 3.10+, standard library only. From this directory:

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -v
python examples/network_demo.py
python examples/chapter_demo.py --output benchmarks/features-2026-10-26.json
python tools/benchmark.py --output .runtime/query.json
```

The full suite/demo uses the sibling chapter 20 queue executable: three real
brokers, one coordinator and three TCP proxies, plus exporter, collector, query
API, storage consumer, alert worker and an owned localhost webhook receiver.
There are **13 independent processes** in the full fixture. No simulated Kafka,
external email or commercial alert provider is used. Owned processes stop on exit;
private configs, logs and databases remain in ignored `.runtime/lab-<uuid>/`
directories (queue runtime is under its own module).

The chapter feature demo uses 14 processes, including two collectors with
disjoint static-ring target ownership. Its report checks three active queue
partitions, push replay, weighted gauge aggregation and an owned webhook carrying
severity metadata. This is separate from the original 13-process fault fixture.

The storage/alert unit tests need no queue service. A deployed installation can
point at a compatible external chapter 20 HTTP queue instead of the sibling source.
Optional package install in a virtual environment: `python -m pip install -e .`.

## Persistent operation

Start chapter 20's three brokers and coordinator first, then generate private
monitor credentials (does not overwrite an existing output):

```powershell
python tools/init_config.py --queue-config ../distributed-message-queue/.runtime/secrets.json
$env:PYTHONPATH = 'src'
python -m metrics_monitor.cli exporter --root .runtime/exporter --config .runtime/secrets.json --port 9822
```

Create a private reloadable targets JSON file `.runtime/targets.json` containing
`[{"instance":"local","url":"http://127.0.0.1:9822/metrics","token":"EXPORTER_TOKEN"}]`,
using exporter_token from the generated configuration. In four other terminals,
set PYTHONPATH in each and run one command per terminal:

```powershell
python -m metrics_monitor.cli collector --root .runtime/collector --config .runtime/secrets.json --port 9820 --targets .runtime/targets.json
python -m metrics_monitor.cli api --root .runtime/store --config .runtime/secrets.json --port 9821
python -m metrics_monitor.cli storage --root .runtime/store --config .runtime/secrets.json
python -m metrics_monitor.cli alerts --root .runtime/store --config .runtime/secrets.json --interval 1
```

Storage/API/alert roles share one local SQLite TSDB file; this is not multi-host
SQL HA. The collector outbox uses fenced claims and FIFO per queue partition.
`queue_partitions` (default 1, max 64), `partition_by` (`metric` or `series`),
`dispatch_workers` and `scrape_workers` (default 4, max 32) are optional configuration.
Queue partition expansion must be explicit; an incompatible topic is rejected.
Changing collector sharding does not move old pending chunks, and applications
must coordinate cutovers if they require cross-partition order.

Default scrape interval is 5s. Targets may override interval, timeout, retries and
trusted labels. A bounded reloadable JSON file is the discovery source; malformed
updates retain the last valid targets and expose an error. `collector_id` and the
shared `collectors` list select deterministic ownership on a 64-vnode-per-member
hash ring. Every collector needs the same membership; this is not consensus,
dynamic membership or automatic failover. The exporter reports actual
process_cpu_seconds_total, python_memory_bytes (tracemalloc allocations, **not RSS**)
and http_requests_total. It does not pretend to measure host utilization.
Failed scrape emits collector_up=0. CPU utilization can be estimated from a
counter rate over a sufficiently populated window.

Visit `http://127.0.0.1:9821/dashboard` and supply the read_token. The dashboard
keeps it in memory only. Webhook destinations are trusted operator configuration:
`destinations: {"local":{"url":"http://owned-host/notify","token":"optional"}}`.
No destination is enabled by the configuration generator. Email/phone/PagerDuty
need a real Provider adapter; absent adapters fail explicitly and eventually dead-letter.

## APIs

| Method/path | Contract |
| --- | --- |
| POST collector /push | push/admin Bearer, Idempotency-Key, samples array (1..1000) |
| GET exporter /metrics | exporter/admin Bearer; JSON sample list |
| GET API /query | read/admin Bearer; metric,start,end, optional match JSON, step, aggregation, group_by JSON array |
| GET API /chart.svg | Same query, actual SVG chart |
| GET API /dashboard | Static chart UI, no embedded credentials |
| POST API /rules | admin Bearer, immutable named rule |
| POST API /silences | admin Bearer, match/start/end, optional id |
| POST API /evaluate | admin Bearer, JSON {}; transitions only, no direct external delivery |
| GET API /alerts | read/admin Bearer, states/delivery attempts/rule errors |
| POST API /compact | admin Bearer; rollup/prune |
| GET /health | Local role, backlog/counts, worker and dispatch errors |

Samples use metric, labels (default {}), type gauge/counter, timestamp Unix seconds,
value. At most eight labels, 128 characters per value; default 10,000 total series,
1,000 per metric, exact-match queries limited to 200 series/20,000 output points.
No regex selector or full PromQL support. Nonfinite values, negative counters,
type conflicts and conflicting immutable timestamps fail atomically.

Push acceptance means persisted in collector outbox, **not yet in TSDB**. 16MiB
default backlog is bounded using a transactional byte counter; 429 signals
backpressure. Queue records are <=12KiB, up to 100 samples each. Chunks are grouped
by metric or metric+labels; per-partition producers preserve the exact payload
after an uncertain result. A partition's failed head does not stop other partition
heads. `Dispatcher` owns a bounded executor and drains accepted publication work.
Storage consumers rotate through their group assignments, applying data before queue commit; duplicates after
crash/lost ACK are safe. Invalid batches/cardinality violations enter a visible
hash/reason rejection ledger; they are not retried forever. Queue offset expiry
fails closed and requires explicit operator reset/recovery. Its coordinator is
still a SPOF. Collector receipts/rejection receipts retain 14 days; successfully
dispatched collector payloads are cleared. Some durable delivery/alert metadata
does not yet have GC.

## Query/retention semantics

Range queries use exact labels and inclusive raw sample times. Gauge aggregations:
avg/min/max/last; averages are sample-weighted, not time-weighted. Counter queries
use increase/rate/last, accounting for resets. Rate is per bucket increase divided
by observed first/last time; one timestamp yields null, with no extrapolation or
implicit cross-step lookback. Counter cumulative readings are not summed as counts.

Nominal tiers: raw 7d, minute 30d, hour 365d. Bucket alignment can retain raw an
extra minute and minute/hour data an extra hour. Late raw samples within that
window are accepted and rollups recomputed before pruning; future tolerance 30s.
Queries combine nonoverlapping tiers and recent live tails. Older requests require
steps divisible by retained grain (60 or 3600). A partial coarse bucket includes
its whole summary and is disclosed as partial_coarse_buckets; finer boundary
precision cannot be recreated. Cache entries are bounded and revision/range keyed.

`group_by` omitted preserves separate series; `group_by=[]` merges all matching
series, or supply label names such as `["zone"]`. Cross-series gauge avg/min/max
are supported; avg is weighted by underlying sample counts. Grouped counter and
last queries are rejected to avoid inventing reset/ordering semantics. Grouping
does not bypass the 200 source-series limit.

Ingestion freezes inputs before waiting for the writer and records dirty minute
and hour buckets. Compaction computes only affected summaries from a read snapshot
outside the writer, then publishes with a revision check (up to three attempts).
Its per-pass budgets are 20,000 minute/hour keys per tier and 200,000 source atoms;
over-limit or repeatedly changing snapshots return 503 without pruning source data.
Queries cap input at 200,000 atoms globally (20,000 raw points per series) and
output at 20,000 points; over-limit returns 413. Cache cold loads coalesce on
fixed stripes and every result is isolated from caller mutations. Cardinality
budgets persist with each database; this is a bounded SQLite reference engine.

Cold-hour export is a local gzip/checksummed archive, not cloud tiering:

```powershell
python -m metrics_monitor.cli archive --root .runtime/store --config .runtime/secrets.json --start UNIX_START --end UNIX_END --output .runtime/cold.json.gz
```

Use completed hour-aligned ranges older than 30 days. `read_archive(path)` verifies
format/checksum, identities, bucket ranges and statistics. Export/read are bounded
to 100,000 buckets, 10,000 series and 64MiB compressed/decoded content. Exclusive
creation avoids overwrites; a failed write removes only the newly created file
with the same filesystem identity. Archives are outside
automatic live retention and need separate lifecycle management.

## Alert semantics and verified limits

Rule fields: id/metric/op/threshold/destination; optional match, aggregation (last),
for_seconds (0), window/max_age (300s), group_by ([instance]), channel (webhook).
Rules have immutable IDs; identical replay is allowed. Thresholds evaluate per
matching series. False resets pending/resolves firing; stale/missing data resets
pending but retains firing with no_data=1. No auto recovery is invented.

Use `--rules-file path.json` (`--rules` alias) on API/storage/alerts roles to load
an array of rules or `{"rules":[...]}` before workers start. Import is atomic,
up to 1000 rules/1MiB, and rejects duplicate/conflicting IDs. It is JSON rather
than a full YAML/PromQL parser. Optional rule labels can attach severity metadata.

Silences match series labels and delay sends, while evaluation continues. Unbatched
superseded transitions are discarded. Once a group delivery is frozen, retries
reuse its exact payload/key; groups use FIFO, 30s leases and up to five attempts
with bounded exponential backoff. A newly added silence can delay an entire mixed
batch. Already materialized historical transitions may be delivered after silence
in order. A resolution can be sent even when its firing event was suppressed.
Without receiver idempotency, external delivery is at least once.

HTTP defaults to 32 accepted handlers, backlog 128 and a 10-second absolute socket
budget; CLI exposes `--concurrency` and `--request-timeout`. Bodies are limited to
1MiB and responses to 4MiB. Authentication precedes body reads; duplicate framing,
duplicate JSON fields and nonfinite numbers are rejected. Direct outbound HTTP
does not forward credentials through redirects/environment proxies. Non-daemon
workers/handlers drain accepted work on close; DNS/SQLite/CPU cannot be forcibly
interrupted by a socket deadline. Background failures remain visible and retry.

Final regression, actual process demos and raw performance are linked in
[validation](benchmarks/results.md). The 2026-10-20 15-test/67.171ms results remain
historical, not this round's totals. Production TSDB encoding/sharding, consensus
discovery, metadata HA, Kafka/Flink deployment, an external alert event bus, full
PromQL/Grafana, TLS, real provider credentials, cloud deployment and the book's
10 million series remain extensions. Notifications use the local durable outbox;
only the metrics stream currently traverses the chapter 20 queue.
