# Requirements: Metrics Monitoring and Alerting (chapter 21)

Source: [System Design Interview.md](<../../System Design Interview.md>), lines
8997–9663. Review date: 2026-10-26. Infrastructure gauges/counters only; logs,
traces and business analytics remain outside scope. Final execution results:
[verification](../benchmarks/results.md); gaps: [chapter review](chapter21-review.md).

## Executable core

- Push and reloadable pull target discovery, bounded concurrent scrapes, trusted
  labels, target interval/retry deadlines and visible failure/up metrics. Static
  consistent hashing assigns targets with explicit common collector membership.
- A durable capacity-bounded collector outbox accepts before queue publication.
  Metric/series sharding, fenced partition-FIFO claims and bounded Dispatcher
  allow different partitions to progress. Stable batches deduplicate storage
  after transport duplicates, and temporary failures never acknowledge lost work.
- Actual chapter 20 HTTP brokers/independent disks provide queue transport. Group
  consumers rotate assignments; expired offsets require explicit recovery.
- SQLite reference stores immutable samples, exact label indexes, bounded series,
  consistent revision queries and isolated/coalesced cache results. Rollups rebuild
  dirty buckets outside the writer and publish with revision validation.
- Raw ~7d, minute ~30d, hour ~365d; late data within raw retention recomputes
  affected summaries before pruning. Counter resets compose across tiers. Gauges
  support weighted cross-series avg/min/max by an explicit label grouping.
- Bounded complete-hour local gzip archives include hash/schema/identity checks,
  exclusive/fsynced creation and failure cleanup; cloud storage is not implied.
- Immutable rules/atomic JSON file import, static severity labels, bounded snapshot
  evaluation, pending/firing/resolved/no-data policy, silence, stable notification
  grouping, leases, attempt/backoff limits and visible dead records.
- Real owned HTTP webhook delivery, with explicit Provider boundaries for
  email/phone/PagerDuty. Receiver cooperation is required for external deduplication.
- Authenticated query/SVG/dashboard; bounded HTTP admission/framing/JSON/deadlines,
  direct outbound transport and owned-worker shutdown/drain.

## Limits and acceptance

Default series limits are 10000 total/1000 per metric, at most eight labels.
Queries allow 200 source series/200000 input atoms/20000 output points. Rollup
passes have 20000 keys per tier/200000 atoms and three revision retries. Archives
allow 100000 buckets/10000 series/64MiB compressed and decoded. Over-limit work is
explicitly rejected; no result may claim production-scale storage or query SLA.

Actual process/network examples are separate from controlled clock/transport
fault tests. The base fixture has 13 processes; the chapter feature fixture uses
14, including two collectors with distinct ring ownership. Validate replay,
no-data, retained-tier seams, late updates, grouping and lost-ACK notification IDs.
Final totals and raw measurements must come from current execution, not historical
2026-10-20 evidence. No external notification is authorized by configuration alone.

## Remaining production requirements

Distributed/compressed production TSDB, consensus discovery/HA, dynamic collector
failover, priority streaming/Flink, separate alert event bus, full PromQL/Grafana,
TLS/ACLs, real provider credentials, cloud retention, metadata GC, backup recovery,
sustained 10-million-series capacity and power-loss verification remain extensions.
SQLite and the queue's single coordinator are explicit deployment limits.

Commit only after user request; requirements/tasks retain the local no-commit
convention. Design changes are eligible for a later authorized commit.