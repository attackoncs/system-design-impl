# Tasks: Stock Exchange

Source: Chapter 29 of `System Design Interview.md` in the root directory, starting at line 15039.

Status: Reference implementation complete; 56 tests, 4-process demo, and p99 measurement passed. Simulated accounts/market data only; asynchronous primary/backup matching and manual epoch switching verified, sequencer remains a single point, no automatic leader election or production-scale proof.

## Scope

Limit-order equity new/cancel, real-time fills and order books, trading session and risk checks, fund freezing; uses simulated market/account data.

## Implementation Tasks

- [x] T1. Write requirements/design, define integer price units, orders/fills, sequence numbers, frozen funds, and deterministic matching rules.
- [x] T2. Implement entry validation, conservative per-user/symbol/day accepted-quota limit, and fund/position freezing.
- [x] T3. Implement price-time priority limit order book, partial fills, cancel, and frozen-resource release on remaining quantity.
- [x] T4. Implement totally ordered sequencer, event log, per-symbol logical order book, and deterministic replay; per-symbol independent process parallel sharding not implemented.
- [x] T5. Implement fill/market publication, book deltas, snapshots, candlesticks, and resynchronization after missed deltas.
- [x] T6. Implement asynchronous primary/backup replication, snapshot/recovery, epoch publication fencing, and report stream; sequencer replication/automatic switching remains an unimplemented extension.
- [x] T7. Preserve interfaces for identity/risk/session rules; do not claim exchange compliance certification; do not send orders to real trading venues.
- [x] T8. Verify fair ordering, same-price first-come-first-served, cancel-versus-fill races, fund conservation, failure replay, and market recovery; report p99 latency.
- [x] T9. Provide runnable examples, English/Chinese README, design tradeoffs, and actual test/stress reports; update root status; leave unvalidated production scale explicitly unproven.

## Acceptance Thresholds

- Correctness, persistent restart, idempotency, and failure paths have reproducible tests.
- Distributed capabilities must be verified with actual independent processes/network; simulated implementations are marked separately.
- External cloud services/providers use replaceable adapters; do not claim real services verified when credentials are not configured.
- Original-book scale and availability numbers are design goals, not directly proven by local testing.
- Submit only upon user request; requirements.md/tasks.md under docs continue the convention of not being submitted.

## 2026-10-27 review

- [x] Chapter/graph mapping; contracts/policies/index/audit/ingestion/queries/transport modules and bilingual docs.
- [x] Price-level/FIFO index, dense execution sequences, book-style aliases and execution/candle time filters.
- [x] Frozen commands/durable deduplication, owner-flow/fill/index audit, full-state promotion, epoch/prefix checks.
- [x] Reject market conflicts/rollback/invalid quantities, atomic cursors/receipts, legacy upgrade and safe concurrent snapshots.
- [x] Concurrent reads, rate/capacity/protocol/deadlines, owned polling shutdown, 56 tests and recorded measurements.
- [ ] Raft/elections/sequencer replicas, cross-host recovery and separate outbound event bus.
- [ ] mmap/SBE/FIX, multicast fairness/L3, CPU pinning, snapshot acceleration/compaction and production scale.
