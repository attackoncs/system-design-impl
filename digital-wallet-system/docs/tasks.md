# Tasks: Digital Wallet

Source: Chapter 28 of `System Design Interview.md` in the root directory, chapter starting at line 14171.

Status: Reference implementation complete; 54 tests and the 7-process HTTP demo/measured runs pass. All balances are synthetic; fixed-coordinator journal majority and persistent epoch fencing are verified. Automatic leader election, account shard replication, and coordinator original-disk-loss recovery are not implemented; one million TPS / four nines are unproven.

## Scope

Two-wallet balance transfers, transactional correctness, and reproducible historical state; no FX. One million TPS and four nines are design targets.

## Implementation Tasks

- [x] T1. Write requirements/design defining accounts, integer monetary units, transfer commands, event logs, balances, and conservation invariants.
- [x] T2. Implement single-partition atomic transfers, insufficient-balance rejection, idempotent commands, and concurrent-debit protection.
- [x] T3. Implement account sharding and cross-partition transfers; compare 2PC, TCC, and Saga, and make recovery states explicit after selecting the strategy.
- [x] T4. Implement append-only event logs, deterministic replay, CQRS balance reads, and historical balance reconstruction.
- [x] T5. Implement snapshots, file-based log/state persistence, checksums, and restart recovery; verify replay does not debit again.
- [x] T6. Implement fixed-coordinator epoch fencing, a three-node real-HTTP journal majority, and same-disk recovery; automatic leader election / account replicas remain unimplemented extensions.
- [x] T7. Implement cross-shard pending/confirm/cancel recovery, audit trails, and internal reconciliation; no connection to real money.
- [x] T8. Verify concurrent/duplicate transfers, partitioning/crashes, partial commits, replay consistency, and balance conservation; measure and state that one million TPS is unproven.
- [x] T9. Provide a runnable example, English/Chinese READMEs, design trade-offs, and actual test/stress-test reports, and update the root status; unverified production scale remains explicit.

## Acceptance Criteria

- Correctness, persisted restarts, idempotency, and failure paths have reproducible tests.
- Distributed capabilities must be verified with real independent processes/networks; simulated implementations are labeled separately.
- External cloud services/vendors use replaceable adapters; without configured credentials, real-service verification cannot be claimed.
- The original book's scale and availability figures are design targets, not directly proven by local tests.
- Commit only after the user requests it; requirements.md/tasks.md under docs continue the convention of not being committed.

## 2026-10-27 review

- [x] Map chapter/graph, extract contracts/reducers/runtime/audit/transport, synchronize bilingual docs.
- [x] Freeze before writer waits, validate participant identity, incrementally cache committed facts, pin read snapshots and safe snapshot publication.
- [x] Parallel replica RPC, shared append/sync ownership, exact prefix commit, bounded completion waits, capacity/deadline/cancellation/drain.
- [x] Failure/race/protocol and seven-process notification recovery tests; retain HEAD baseline and raw measurements.
- [ ] Raft elections, participant event replication and authority-disk-loss recovery.
- [ ] Independent CQRS readers, accelerated/compacted snapshots, shard migration and production-scale validation.
