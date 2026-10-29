# Tasks: Distributed Message Queue

Source: [System Design Interview.md](<../../System Design Interview.md>), chapter 20,
starting at line 8020. Core review: 2026-10-26. Final test/performance evidence is in
[validation](../benchmarks/results.md); coverage and remaining gaps are in
[chapter review](chapter20-review.md). Historical 2026-10-20 results are preserved
under their original date. No production-complete or commit/push claim is made.

## Reference implementation

- [x] T1. Requirements/design distinguish partition ordering, confirmation points,
  protocols, delivery semantics and single-coordinator ownership.
- [x] T2. Segmented integrity-checked logs, offset index, complete batch validation,
  coalesced writes/fsync, safe generation switches and time/byte retention.
- [x] T3. Key-aware producer routing, send_many, fixed per-partition lock stripes,
  sequence/payload recovery, size/linger buffering, bounded Futures and close.
- [x] T4. Pull groups, heartbeat/leave, rebalancing, durable cursors and fencing;
  revalidate membership after broker reads.
- [x] T5. Real broker leader/follower replication, committed-prefix and response
  verification, majority publication and bounded full-snapshot catch-up.
- [x] T6. At-least-once / at-most-once; atomic queue output visibility plus input
  cursor with txid replay, excluding external side effects.
- [x] T7. Retry/dead-letter, relative/absolute delivery, OR tags/scanned cursors,
  long polling, increasing partition counts and bounded local archive/replay.
- [x] T8. Remove global I/O serialization; use ordered operation stripes, short
  metadata transactions, coalesced broker loads and per-log locking.
- [x] T9. Bound HTTP admission/framing/JSON/RPC; drain accepted handlers/client
  work/maintenance; retry failures with sanitized observable status.
- [x] T10. Preserve real independent-process fault tests and add focused regression
  for confirmed concurrency, durability, client and protocol issues.
- [x] T11. Update English/Chinese READMEs/design and local requirements/tasks;
  link final executable evidence rather than reusing historical test totals.

## Acceptance criteria

- Correctness, restart, idempotency, transactions and failure paths have repeatable
  behavior tests. Exact final counts/timings come only from actual runs.
- Real distributed claims require independent processes and networking; controlled
  in-process faults are labeled.
- Local benchmarks do not establish production throughput/availability or every
  filesystem's power-loss guarantee.
- docs/design.md is eligible for an authorized commit. Requirements/tasks remain
  local under the existing convention; commit/push require the user's request.

## Unfinished production extensions

- [ ] P1. Consensus metadata, coordinator HA/failover, regional recovery and
  backup/restore validation.
- [ ] P2. Dynamic broker membership/placement, partition migration/shrinking and
  ordering-preserving key reassignment.
- [ ] P3. Incremental/chunked catch-up, full dynamic ISR protocol, zero-copy and
  stronger resource/index bounds at large retained volumes.
- [ ] P4. TLS deployment, topic ACLs/quotas, receipt/epoch GC, cloud tiering and
  sustained production load/chaos validation.
- [ ] P5. Power-loss/controller faults on target filesystems; current process-crash
  tests do not imply universal durability.
