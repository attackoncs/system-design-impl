# Design: Metrics Monitoring and Alerting

## Pipeline and process boundary
An HTTP collector accepts push batches and periodically discovers/scrapes JSON
exporters. It commits a bounded SQLite outbox before replying. Stable chunk IDs
and content hashes survive restarts. A dispatcher sends <=12KiB queue records
through the chapter 20 HTTP protocol with quorum confirmation. Queue transport
is external to this package: three actual brokers and a coordinator are used in
the lab; no Kafka/Redis deployment is simulated. A storage worker consumes using
fenced group leases, applies batches once in the TSDB, then acknowledges the queue.
Lost acknowledgement or a new producer epoch may cause transport duplicates;
durable batch receipts make repeated ingestion idempotent. Invalid batches go to
a durable local rejection ledger (hash/reason, not an endlessly retried poison).

## Series and aggregation
SQLite WAL stores series, an inverted exact-label index, immutable raw samples,
minute/hour statistical summaries and ingestion receipts. A write transaction
enforces series budgets and updates revision. Concurrent conflicting samples roll
back the batch. Late raw samples trigger rollup recomputation; complete buckets
are rebuilt from retained lower-resolution data before pruning. A summary retains
count/sum/min/max, first/last values and times, and internal counter increase.
Combining counter summaries adds the boundary delta, using the new value after a
reset. Gauge mean uses sum/count; counter rate uses increase/(last-first), no
extrapolation, and requires at least two sample times. It is not full PromQL.

Raw cutoff is floor((now−7d)/60)*60; minute cutoff is
floor((now−30d)/3600)*3600; hour cutoff is floor((now−365d)/3600)*3600.
Retention can exceed nominal ages by one lower-tier bucket to avoid seam loss.
Queries select nonoverlapping age tiers, combine by requested step and disclose
coarse partial-bucket overfetch. Coarse query steps must align with retained grain.
Recent raw data fills live tails. Cache keys include storage revision and the
complete selector/range/options. No production TSDB encoding/compression is claimed.

## Alerting and delivery
Rules are immutable named configurations (identical replay allowed). Each matching
series has a durable state and monotonic transition sequence. A false evaluation
resets pending or resolves firing; missing/stale data resets pending and keeps an
already firing alert active with no-data status, avoiding invented recovery.
Notification grouping uses rule ID and configured label keys. State transitions
and pending notification events commit together. Silences delay notifications,
while evaluation continues. Events not yet batched may be cancelled if superseded.
Once grouped, a delivery has a frozen payload and stable idempotency key; retries
cannot accidentally regroup it under a different key. Per-group FIFO prevents a
resolution overtaking an earlier retry. Leases fence competing delivery workers,
backoff is bounded and exhausted deliveries remain visible as dead letters.

Webhook transport is real HTTP with optional destination credentials. Provider
interface boundaries cover email/phone/PagerDuty; absent adapters fail explicitly,
never pretend to deliver. Receiver idempotency handles the tested lost-ACK case.
End-to-end external delivery is at least once, not exactly once without receiver
cooperation. API/worker roles may share one local TSDB file; this is not a remote
SQL cluster. Collector root and dispatcher are single-owner reference components.

## Operations and extensions
Read/push/admin credentials are separate. Dashboard asks for a read token and keeps
it in memory; no embedded secrets or persistent browser token storage. Discovery
and notification destinations are trusted operator configuration, not arbitrary
untrusted URLs. CPU is process CPU seconds, memory is traced Python allocation,
not host utilization/RSS. Service failure emits collector_up=0. The reference needs
consensus metadata, scalable TSDB/stream partitions, real provider credentials,
TLS/ACLs, quotas/receipt GC, disk compaction, query planning and sustained-load
validation before production. Queue's single coordinator remains a known SPOF.
