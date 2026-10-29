# Tasks: Ad Click Event Aggregation

Source: Chapter 22 of `System Design Interview.md` in the root directory, chapter starting at line 9664.

Core review: 2026-10-26. Current regression and measurement results are recorded
in [the verification report](../benchmarks/results.md); chapter coverage and
production gaps appear in [chapter22-review.md](chapter22-review.md). The earlier
2026-10-20 baseline had 11 tests and a six-process interrupted-transaction demo.

## Scope

Count advertisement clicks over the past M minutes and per-minute Top N (default 100), with filtering by user/IP/country; handle late events, duplicates, and partial failures.

## Implementation Tasks

- [x] T1. Write requirements/design, defining event IDs, event time, filter dimensions, windows, and result revision semantics.
- [x] T2. Implement appended JSONL ingestion, a single-node persistent partitioned HTTP log, plus validation and replay endpoints; no replicated-queue HA claims.
- [x] T3. Implement stream aggregation with event-ID salting across ad/minute dimensions, watermarks, late tolerance, and closed-window corrections.
- [x] T4. Implement event deduplication, checkpoints, and output transactions; real processes exiting before commit recover without double counting.
- [x] T5. Implement windowed count queries, exact global Top N, and filter intersections; dimensional costs and candidate budgets are documented.
- [x] T6. Implement 4 partitions, 4 independent workers/stores, and a two-stage merge; fixed topology, with dynamic scaling/migration remaining a production extension.
- [x] T7. Implement raw-log rebuild, independent batch reconciliation, hash-based anomaly detection, and backlog/error status; no reliance on third-party monitoring vendors.
- [x] T8. Verify duplicates/late events, checkpoint interruption, node exit, hot keys, and failure replay; measure query latency and count correctness.
- [x] T9. Provide runnable examples, Chinese and English READMEs, design docs, and benchmark reports; update root-directory status; production scale remains unverified.
- [x] T10. Reuse bounded query fan-out/admission and one deadline; validate source/
  worker responses and select exact heap Top N after a complete candidate merge.
- [x] T11. Freeze/validate pages before writer locks, verify overlap positions,
  preserve failure rollback and source/checkpoint identities with atomic publication.
- [x] T12. Add HTTP/RPC deadline/framing/JSON/query budgets, start workers after bind,
  surface retry failures and drain accepted handlers/executors/polls on shutdown.
- [x] T13. Bound independent reconciliation/replay and add meaningful storage,
  query/recovery, protocol and lifecycle regressions; update both documentation languages.

## Acceptance Criteria

- Correctness, persistent restart, idempotency, and failure paths have repeatable tests.
- Distributed capabilities must be verified with actual independent processes/networks; simulated implementations are labeled separately.
- External cloud services/vendors use replaceable adapters; when credentials are not configured, real-service verification must not be claimed.
- The book's scale and availability numbers are design targets, not directly proven by local tests.
- Commit only after the user requests it; the convention of not committing docs/requirements.md and docs/tasks.md continues.

## Acceptance Evidence and Remaining Boundaries

The 2026-10-26 final count, durations, independent-process results and raw
measurement links live in [benchmarks/results.md](../benchmarks/results.md).
The items below are historical **2026-10-20** evidence, not current measurements.

- 7 core tests + 4 real six-process network tests pass; after os._exit(73) before transaction commit, counts and checkpoints roll back together.
- 3,000 events distributed 751/738/745/766; full aggregation took about 0.736 seconds; 40 queries at 4-way concurrency p95 81.294ms.
- Reconciliation independently computes full-dimensional hashes; deliberately corrupting online results is detected as inconsistent. See ../benchmarks/results.md for details.
- Single-node raw log and SQLite lack replicated HA; cross-machine load testing, online partition expansion, automatic privacy cleanup, and production batch processing remain to be implemented.
- Chapter code is complete locally, not yet committed or pushed.

## Remaining production work

- [ ] Replicated raw log/automatic worker failover, dynamic partition migration,
  original-scale sustained workload and host/disk-loss recovery validation.
- [ ] A second output queue/database-writer and physical distributed DAG, with an
  explicit external sink transactional contract before broader exactly-once claims.
- [ ] TLS/tenant controls, privacy lifecycle, dedup/state GC, cloud cold storage and
  a scalable distributed batch engine rather than the bounded local recovery tool.
