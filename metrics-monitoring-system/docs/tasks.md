# Tasks: Metrics Monitoring and Alerting

Source: [chapter 21](<../../System Design Interview.md>), lines 8997–9663.
Core review: 2026-10-26. Final tests/measurement are recorded in
[verification](../benchmarks/results.md); [coverage and gaps](chapter21-review.md).
No commit/push is implied. Historical 2026-10-20 totals are not current totals.

## Reference work

- [x] T1. Separate collection/outbox, discovery, queue adapter, query/rollups,
  archive, alert configuration/evaluation/delivery, HTTP and lifecycle concerns.
- [x] T2. Bound target reload and parallel scrapes; static ring ownership, trusted
  labels, retry budgets and visible failures; retain valid targets on bad reload.
- [x] T3. Persist collector capacity counters and partition-FIFO leases; bounded
  parallel dispatch, per-partition exact uncertain retry and group assignments.
- [x] T4. Freeze ingest inputs before writer waits; enforce cardinality, sample
  conflicts and transaction rollback. Keep temporary failures unacknowledged.
- [x] T5. Isolate cached results/coalesce loads; bounded snapshot queries and
  cross-series sample-weighted gauge avg/min/max with explicit group_by.
- [x] T6. Track dirty rollup buckets, compute outside writer and revision-check
  publication; preserve counter resets, late repairs and retention-tier seams.
- [x] T7. Bound archive creation/decompression, verify structure/identity/checksum,
  fsync new files and clean only matching newly created files on failure.
- [x] T8. Import immutable JSON rule files atomically before workers; attach static
  labels without changing selectors; bounded snapshot evaluation/state transitions.
- [x] T9. Fence delivery claims/results, count crash attempts at claim, preserve
  stable grouped payloads/keys and bounded FIFO retry/dead-letter behavior.
- [x] T10. Bound HTTP admission/framing/JSON/deadlines; direct transport and verified
  HTTPS; bind before start, drain owned handlers/workers, expose retry failures.
- [x] T11. Preserve actual queue replay and webhook lost-ACK fixture; add behavior
  regressions and the two-collector/partition/grouping/severity chapter demo.
- [x] T12. Synchronize EN/CN README/design and local requirements/tasks; link actual
  execution evidence, preserve historical benchmark dates and production limits.

## Acceptance

Correctness/failure/concurrency tests are reproducible. Network claims require
independent processes and real sockets; controlled substitutes are labeled.
Current test counts and raw measurements come from benchmarks/results.md only.
External providers are not claimed as delivered without configured adapters.
Requirements/tasks remain local until a user-authorized change to that convention.

## Remaining production work

- [ ] P1. Consensus metadata/discovery, automatic failover and multi-host TSDB HA.
- [ ] P2. Production TSDB compression/shards, priority stream processing, full
  query language and separate scalable alert-event transport.
- [ ] P3. TLS/ACL, commercial notification providers, cloud cold storage and backups.
- [ ] P4. Comprehensive receipt/alert metadata GC, quotas and target-scale soaks.
- [ ] P5. Target-filesystem power-loss and disaster-recovery validation.