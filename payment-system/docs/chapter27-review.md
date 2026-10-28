# Chapter 27 review — 2026-10-27

Read local book lines13482–14170. Pay-in/payout, hosted token simulation,
partial refunds, double-entry ledger, wallet, retries, outbox and reconciliation
already existed. Added trusted bounded multi-order checkout and the payment GET
alias; hardened identity/revision/lease/statement contracts and module ownership.
The acceptance SQL transaction spans all checkout operations/jobs; external
merchant orders still authorize and settle independently. No real money moved.

Baseline13tests16.541s. Final48passed21.561s,-Werror,no skips. Five network tests
launch four independent processes. New tests cover batch rollback/idempotency,
writer-wait input mutation, expired-but-unreclaimed jobs, same-revision conflicts
after UNKNOWN, ledger identity mismatches, corrupt/duplicate statements,
reconciliation ownership/result replay, reclaimed callback ACKs, mismatched
provider operation IDs, direct RPC/framing/limits and owned thread shutdown.

Manual dependency trace:HTTP→Store.checkout/operations→job queue→worker→mock PSP;
worker receipts and signed PSP callbacks converge on Store.apply→Ledger.post→
wallet/state/outbox in one transaction; notifications→Receiver durable dedup;
leased reconciliation→strict statement→local audit/review. New extracted modules
retain the facade interface; there are no cross-chapter runtime imports.

New demo:[feature JSON](../benchmarks/features-2026-10-27.json). Two merchant
orders:SUCCEEDED/FAILED,identical IDs on retry,payout300/refund200,available500,
held0,other wallet0,five posted transactions/four unique provider operations,
independent reconciliation true. Original demo:available5000,held0,five postings,
three provider operations and three consumer events,matching audit.

20pay-ins/four clients:2.996590s,6.674253completed/s,p50/p95537.5847/697.3395ms.
Wallet2000/held0,20unique PSP operations and20consumer events; audit matches.
[Raw results](../benchmarks/review-2026-10-27.json). Startup and final outbox/audit
drain are excluded from throughput. This is a short localhost simulation,
not a controlled speedup comparison, sustained capacity or production10TPS proof.

code-review-graph minimal context was used (cached6863nodes/62421edges/544files,
built2026-10-26T20:03:03,matching HEAD). Final build/impact calls were blocked:
'MCP tool call requires approval,but approval policy is never'. No final graph
freshness or complete dependency-coverage claim is made. Manual call-chain
review and actual regression cover new untracked modules; no alternate route
was used to bypass the tool's approval requirement.

Limits:single SQLite writer/authority,no replicated HA,real PSP/risk/AML,
PCI certification,bank settlement,FX/fees/taxes/chargebacks,atomic multi-merchant
external charge,finance resolution UI or large-history streaming/retention.
The revision hash table starts at upgrade and cannot reconstruct unseen historical
payloads. Amounts remain bounded integer USD cents. No commit or push.

Provider transaction IDs are also globally unique across local operations;
reuse enters review before posting. The database unique index is a second guard.
Legacy duplicate bindings must be audited before upgrading.
