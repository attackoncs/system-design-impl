# Tasks: Payment System

Source: Chapter 27 of `System Design Interview.md` in the root directory, chapter start line 13482.

Status: source/contract review completed (2026-11-05); 48 tests passed in22.021s. Both four-process examples and the local benchmark passed. No production or test code changed this round; all payments use a local mock PSP, without real rails or authority HA.

## Scope

Single-currency e-commerce pay-in, payout to sellers, third-party PSP interface and reconciliation; does not persist card numbers, uses only mock/sandbox providers.

## Implementation Tasks

- [x] T1. Write requirements/design, define order/payment/execution records, wallet, double-entry ledger and minimum amount unit.
- [x] T2. Implement pay-in/pay-out state machine, request idempotency key, provider token reference and hosted checkout interface.
- [x] T3. Implement PSP mock adapter, webhook verification/deduplication and out-of-order state update; real charging/payout is out of scope for this task.
- [x] T4. Implement double-entry immutable ledger, validate debit/credit balance and correlate business/provider transaction IDs.
- [x] T5. Implement UNKNOWN state after network timeout, proactive query, exponential retry, retry queue and dead letter; forbid blind duplicate charging.
- [x] T6. Implement asynchronous reconciliation across internal ledger/payment records/provider statements and manual review queue.
- [x] T7. Implement outbox and other internal event-consistency mechanisms, make explicit the distinction between one-time business effect and at-least-once network delivery.
- [x] T8. Verify duplicate/fake callbacks, payment delay, failure recovery, duplicate-payout suppression, ledger invariants and reconciliation drift.
- [x] T9. Provide runnable example, English/Chinese README, design trade-offs and actual test/load-test report, update root directory status; keep the unverified production scale explicit.

## Acceptance Thresholds

- Correctness, persistence after restart, idempotency and failure paths have reproducible tests.
- Distributed capability must be verified with actual independent processes/network; mocks are labeled separately.
- External cloud services/providers use replaceable adapters; without configured credentials, real services must not be claimed as verified.
- The scale and availability numbers from the original book are design goals and are not directly proven by local tests.
- Submit only when the user requests; continue the convention that `requirements.md` / `tasks.md` under docs are not submitted.

## 2026-10-27 review

- [x] Trusted1..20order checkout with atomic acceptance and fixed per-order IDs.
- [x] Frozen inputs, post-lock clock, exact ledger identity and provider-version hashes.
- [x] Four executors, lease expiry/token fences and operation/event receipt identities.
- [x] Complete bounded statement integrity and fenced immutable reconciliation results.
- [x] Strict bounded direct HTTP, bind-before-background startup and cancellation/drain.
- [x] 48tests21.561s, four-process demos and20payment/four-client measurements.
- [ ] Production provider/risk/banking, HA, security, streaming/retention and finance resolution.

## 2026-11-05 source and contract review

- [x] Read the chapter, inspect CodeGraph paths and compare pinned Hyperswitch/TigerBeetle source; [mechanisms and boundaries](open-source-review_cn.md).
- [x] Review checkout, operation/receipt identity, balanced ledger/wallet, refund holds, leases, callbacks, outbox and reconciliation. No confirmed new production defect; production/test source unchanged.
- [x] Correct cumulative counted failures versus total attempts/PENDING polling, network30s/direct2s leases, and multi-order local acceptance versus independent external settlement.
- [x] Strict full acceptance:48 tests22.021s, no skips; five tests use actual four-process HTTP. Controlled-RPC tests do not substitute for real PSP validation.
- [x] Run both examples and20-payment/four-client benchmark sequentially after tests; synchronize bilingual docs, root indexes and the learning guide. See [measurements](../benchmarks/REPORT.md).
