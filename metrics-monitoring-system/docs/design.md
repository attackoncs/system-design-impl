# Design: Metrics Monitoring and Alerting

Core review: 2026-10-26. Source: chapter 21, lines 8997–9663. Final results:
[verification](../benchmarks/results.md); [chapter mapping](chapter21-review.md).

## Collection and transmission

HTTP push and discovered JSON exporters feed a SQLite outbox before acceptance.
A transactional byte counter enforces the default 16MiB backlog; full backlog is
429. Metric or metric+normalized labels determine the queue partition. Chunks
contain at most 100 samples/12KiB and retain their target after config changes.
Owner/lease claims enforce FIFO within a partition while Dispatcher runs distinct
heads concurrently in a bounded executor. Unknown queue results retain the same
producer sequence/payload. New producer epochs may duplicate transport; durable
TSDB batch receipts remain the apply-once boundary.

Storage consumers rotate over fenced group assignments, applying before commit.
Invalid payloads/cardinality failures enter a durable rejection ledger; temporary
storage failures leave the queue cursor unchanged. Queue offset expiry fails
closed. Chapter 20 transport uses actual independent HTTP brokers and disks,
not simulated Kafka; it retains that project's single coordinator limitation.

Reloadable target JSON supports labels, interval, timeout and retries. Invalid
updates retain the last valid list and expose errors. Static consistent hashing
assigns instances across a shared collector list; all collectors must receive the
same membership. It is not consensus discovery or automatic replacement of failed
members. Scrape retries share one target time budget and bounded workers prevent
one slow target from occupying the entire collector indefinitely.

## Storage, query and retention

SQLite WAL stores normalized series, exact inverted label indexes, immutable raw
samples, minute/hour statistics and ingestion receipts. Inputs are frozen before
waiting for the writer; clock/range validation occurs after writer acquisition.
Stored cardinality budgets persist with each database. Conflicts roll back the
whole batch. No production TSDB encoding/compression or physical sharding is claimed.

Dirty work tables track changed minute/hour buckets. Compaction reads a consistent
snapshot, computes/encodes outside the writer, then publishes only if revision
still matches. Up to three attempts, 20,000 bucket keys per tier and 200,000 source
atoms bound a pass; failure preserves unpruned data and returns 503. Migration
seeds available lower tiers once without changing existing batch fingerprints.

Raw cutoff is floor((now−7d)/60)*60; minute/hour cutoffs are floor((now−30d)/3600)*3600
and floor((now−365d)/3600)*3600. Lower-tier complete buckets are rebuilt before
pruning. Queries use nonoverlapping tiers, disclose whole coarse partial buckets,
and require step alignment with retained grain. Gauge mean uses sum/count;
counter increase composes internal and reset-aware boundary deltas. Rate is
increase/(last_time−first_time), needs two timestamps and does not extrapolate.

Query marker/revision and samples share one snapshot. Source limits are 200
series/200,000 atoms (20,000 raw atoms per series), output 20,000 points. Cache
keys include revision and all options; 64 stripes coalesce cold loads and each
returned object is isolated. group_by=None preserves series; [] or label names
merge gauge avg/min/max. Mean is weighted by sample count. Grouped counter/last
is rejected, avoiding invented reset/order semantics; this is not PromQL.

Hourly local gzip archives require complete cold hours, <=100,000 buckets,
10,000 series and 64MiB compressed/decoded content. Reads verify SHA-256, schema,
series identity and bucket/statistic structure. Exclusive creation plus fsync and
identity-checked failure cleanup protects existing files. Archives need separate
GC; there is no cloud upload or retention pin.

## Alerting and delivery

Rules are immutable IDs; bounded JSON arrays or {"rules":[...]} load atomically
before workers via --rules-file/--rules (1MiB/1000 rules). Optional static labels
carry severity without changing metric selectors. Configuration, evaluation,
schema, delivery and providers have separate modules. Evaluation computes from
read snapshots and uses each rule's evaluation-version CAS before publishing
state/events. It does not require an unchanged global Store revision.

False conditions reset pending or resolve firing; no-data resets pending and
keeps firing active, avoiding invented recovery. Notification groups use rule and
configured labels. Unbatched superseded transitions may be cancelled; frozen
batches retain their payload/key across retries. Per-group FIFO prevents recovery
overtaking earlier retries. Leases fence delivery results; attempt count advances
at claim, bounded backoff and five attempts leave exhausted work visibly dead.

Webhook is actual HTTP to an owned receiver; email/phone/PagerDuty need configured
Provider adapters and otherwise fail. Receiver idempotency is needed to avoid
external duplication. The alert delivery outbox is local, not a separate Kafka
alert-event pipeline. Only metrics currently traverse chapter 20's queue.

## Runtime and production boundary

HTTP defaults: 32 accepted handlers, backlog 128, absolute socket budget 10s,
request 1MiB/response 4MiB. Authenticate before body reads; reject duplicate JSON,
nonfinite numbers and ambiguous framing. Direct HTTP avoids environment proxies
and credential-forwarding redirects; configured HTTPS uses certificate checks.
Bind before starting workers; owned non-daemon workers/handlers drain accepted
work and expose retries. Socket limits cannot kill DNS/SQLite/CPU work.

API/storage/alerts may share one local SQLite file; this is not replicated SQL HA.
CPU is process CPU seconds, memory is Python tracemalloc allocations, not host
utilization/RSS. Production needs TSDB distribution/compression, consensus discovery,
stream processing/priority queues, metadata HA, TLS/topic ACLs, real providers,
cloud retention, backups, receipt/alert GC and sustained scale/power-loss validation.
