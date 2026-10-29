# Chapter 27 Payment System Requirements

Source: Chapter 27 of `System Design Interview.md` in the root directory. The original book targets roughly 10 TPS as a design goal; this does not represent the actual verification result on this machine.

- Single currency (USD) with integer cent units: merchant order pay-in, merchant payout, partial refund, simulated hosted checkout; does not accept card numbers or bank account numbers, and does not connect to real payment rails.
- Persists order/payment state, execution records, wallet, double-entry immutable ledger, and provider transaction IDs.
- Dual deduplication by request key and order; provider uses stable operation identity; refund cumulative includes pending refunds; concurrent available balance never goes into overdraft.
- Signed callbacks, time window, event deduplication, old revision ignored, manual review for terminal conflicts / identity mismatches.
- Timeout transitions to UNKNOWN state, proactive query, exponential backoff, lease isolation, dead letter, and manual retry that preserves the original identity.
- Atomic commit of business state, ledger, wallet and outbox; internal messages are at-least-once delivery, consumer is idempotent.
- Asynchronous reconciliation across provider statements, local operations, ledger and wallet; differences are recorded rather than auto-corrected.
- Tests failure recovery through independent-process HTTP, records measured performance and boundaries such as single-authority SQLite and no real PSP/HA.

This reference implementation simplifies to one payment per order; combined multi-order payment, chargebacks, tax, FX and real bank settlement are out of scope.

## 2026-10-27 review

- [x] Trusted1..20order checkout with atomic acceptance and fixed per-order IDs.
- [x] Frozen inputs, post-lock clock, exact ledger identity and provider-version hashes.
- [x] Four executors, lease expiry/token fences and operation/event receipt identities.
- [x] Complete bounded statement integrity and fenced immutable reconciliation results.
- [x] Strict bounded direct HTTP, bind-before-background startup and cancellation/drain.
- [x] 48tests21.561s, four-process demos and20payment/four-client measurements.
- [ ] Production provider/risk/banking, HA, security, streaming/retention and finance resolution.
