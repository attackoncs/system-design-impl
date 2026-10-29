# Tasks: Payment System

Source: Chapter 27 of `System Design Interview.md` in the root directory, chapter start line 13482.

Status: Reference implementation completed; 48 tests pass, a four-process HTTP demo and actual load test have been run. Uses only a local mock PSP; no real payment rails, replica/HA of the authority store, or production-scale validation.

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
