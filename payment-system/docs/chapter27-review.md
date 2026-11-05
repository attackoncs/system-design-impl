# Chapter 27 source and contract review — 2026-11-05

Chapter26 acceptance is complete; no unfinished code or verification work was
carried forward. Read Chapter27 (local lines13482–14170), compared actual pinned
Hyperswitch/TigerBeetle source and inspected CodeGraph paths for checkout,
operation/receipt identity, ledger/wallet, jobs, provider/worker, signed callbacks,
outbox consumer, reconciliation, HTTP and lifecycle ownership.
[Source audit and chapter matrix](open-source-review_cn.md).

The bounded core implementation is present; no confirmed new production-code
defect was found. Production and test source are unchanged. Local acceptance
uses one SQLite transaction for each checkout batch and for final operation/
ledger/wallet/outbox updates; external merchant orders still settle independently.
UNKNOWN and dead jobs cannot by themselves authorize releasing payout/refund holds.
Reconciliation records differences and never auto-corrects money.

Corrected substantive documentation drift:48 current tests, not47; the retry
budget accumulates counted failures, not necessarily consecutive errors.
Normal PENDING polling neither increments nor clears failures, while total
attempts include those polls; administrative retry resets the budget. The
network worker's lease is30s with a shared20s I/O budget, distinct from the
direct helper's2s default. Requirements now distinguish supported1..20-order
atomic local acceptance from unsupported atomic multi-merchant external charges.

Current strict acceptance: **48 tests passed in22.021s**, `-W error`, no skips.
This is one full run, not a before/after comparison. Five tests use four actual
loopback HTTP processes; worker/callback unit tests with controlled RPC are
supporting evidence, not real PSP integration. All money, checkout choices and
bank destinations are synthetic; no card/bank data or real provider was used.

Both actual four-process examples passed. Original flow: payin10000, payout3000,
refund2000, available5000/held0, five ledger transactions, three unique PSP
operations and three consumer events; reconciliation/audit matched. Batch flow:
two merchant orders SUCCEEDED/FAILED independently, same IDs on retry, payout300/
refund200, available500/held0, other wallet0, five ledger transactions and four
unique PSP operations. [Feature JSON](../benchmarks/features-2026-11-05.json).

Twenty pay-ins/four clients:3.159829s,6.329456 completed/s, p50/p95
562.813/732.890ms. Wallet2000/held0,20 provider operations,20 consumer events,
zero pending provider callbacks and matching reconciliation/audit.
[Raw benchmark](../benchmarks/review-2026-11-05.json),
[measurement scope](../benchmarks/REPORT.md). Startup and final outbox/callback/
reconciliation drain are excluded; this is not a speedup or production10TPS proof.

Graph refresh completed with0 files re-parsed and no errors because no source
changed. Explicit dependency scope operations.py/ledger.py/worker.py returned
16 target nodes,16 two-hop impacted nodes and7 additional files, no truncation;
568 unresolved call sites remain. The tool's changed-file labels denote manual
review targets here, not a code diff. The old approval-blocked note below is
historical; current graph access succeeded. Static results do not prove full
coverage or runtime correctness.

Updated bilingual documentation, root indexes and the macro-to-source guide.
No staging, commit or push. Real PSP/risk/AML, PCI certification, bank settlement,
authority HA, TLS, FX/fees/taxes/chargebacks, financial resolution UI and
large-history streaming/retention remain outside the validated reference.

---

# Historical Chapter 27 review — 2026-10-27

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
