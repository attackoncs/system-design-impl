# Tasks: Real-time Gaming Leaderboard

Source: root-directory `System Design Interview.md`, Chapter 26, chapter starting at line 12814.

Status: source/implementation review complete (2026-11-05); 72 tests passed in36.409s with real Redis primary/replica, seven-process global ranking and recovery. No production or test code changed in this review.

## Scope

Monthly-season scoring, +1 per match win, Top 10 and personal ranking; tied users share the same rank, with four neighbors before and after as an extension.

## Implementation Tasks

- [x] T1. Define UTC months, the 48-hour late deadline, event identity, competition rank, and the fixed ten-row truncation.
- [x] T2. Implement the trusted result token, +1 per unique win, durable deduplication, and sharded events; reject client-reported points.
- [x] T3. Implement real Redis dual sorted-sets, tied competition rank, Top 10, and cross-shard personal rank.
- [x] T4. Implement at most four display positions before/after, immutable historical settlement, and zero-score membership in new months.
- [x] T5. Verify real AOF, manual promotion after fencing the old primary, primary/replica recovery, and authoritative journal rebuild; no automatic leader election.
- [x] T6. Document the scope/hash/NoSQL comparison; implement and verify the exact global rank/position algorithm for user-hash sharding.
- [x] T7. Pass tests for concurrent duplicates, commit rollback, cross-month, hard exit, index anomalies, and rebuild consistency.
- [x] T8. Measure end-to-end write/Top 10/personal-window latency with shared read barriers and exclusive writes; local measurements do not establish production targets.
- [x] T9. Complete the seven-process example, Chinese/English READMEs, design docs, benchmark report, and root-directory updates.

## Acceptance Criteria

- Correctness, persistence across restarts, idempotency, and failure paths have repeatable tests.
- Distributed capabilities must be verified with real independent processes/networks; simulated implementations are labeled separately.
- External cloud services/vendors use replaceable adapters; without configured credentials, real-service verification must not be claimed.
- The book's scale and availability numbers are design targets, not directly proven by local tests.
- Commit only after the user requests it; requirements.md/tasks.md under docs continue the no-commit convention.

## Acceptance Results and Boundaries

The test and latency figures below are historical **2026-10-21** evidence. Current
72-test results and measurements are in [the validation report](../benchmarks/results.md).

- 5 authoritative-journal tests + 3 real Redis network tests pass (51.802 seconds); after cleaning up failing HTTP responses, related network cases were re-verified.
- Throughput with all 60 matches fully projected is 2.87/s; at 4 concurrent clients, Top 10 p95 is 2043.169 ms and personal/neighbor p95 is 3662.957 ms, which does not meet the book's production performance targets.
- Real primary hard exit, manual promotion, AOF recovery with projection stopped, and journal rebuild have been verified. See ../benchmarks/results.md.
- Reference limits: 10,000 registered players, single active SQLite authority with shared reads/exclusive writes, manual failover; no cross-datacenter/authoritative HA/online shard expansion.
- This review does not commit or push changes; commit status is separate from chapter acceptance.

2026-10-27 acceptance:72 tests39.110s,real Redis8.0.5/seven processes.
Shared ranking snapshots, exact heads/epochs, bounded RPC/HTTP/RESP, revision
profile cache, frozen journal values, batched zero membership, stale-poll fences
and idle-AOF preservation verified. Production limitations remain documented.

## 2026-11-05 source and implementation review

- [x] Read the chapter, inspect CodeGraph paths and compare pinned WB-Games/Redis8.0.5 source; [differences and bounds](open-source-review_cn.md).
- [x] Review journal, profile cache, candidate/rank logic, projection contracts, Lua, RESP and HTTP/runtime ownership. No confirmed new production-code defect; production and test code unchanged.
- [x] Validate72 tests36.409s with warnings treated as errors, no skips and actual Redis. Do not present SQL-backed RPC unit tests as Redis integration.
- [x] Run the seven-process demo and60-match/40-query-per-kind benchmark sequentially after tests; [measurements](../benchmarks/results.md).
- [x] Correct stale serialized-query wording and explain application user hashing versus Redis Cluster/range/NoSQL alternatives; update bilingual docs, indexes and the learning guide.
