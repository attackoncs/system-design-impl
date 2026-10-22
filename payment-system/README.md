# Payment System — Chapter 27

An executable, standard-library Python reference for single-currency merchant pay-ins, payouts and partial refunds. Four independent processes communicate over actual loopback HTTP: payment API, worker, synthetic PSP with hosted checkout, and an accounting-event consumer. **All money movements are simulated.** No real PSP adapter, card data or bank account numbers are accepted.

## Run

Python 3.10+; no third-party runtime dependencies. From this directory in PowerShell:

```powershell
$env:PYTHONPATH = 'src'
New-Item -ItemType Directory -Force .runtime/tmp | Out-Null
$env:TEMP = (Resolve-Path .runtime/tmp).Path
$env:TMP = $env:TEMP
python -m unittest discover -s tests -v
python examples/network_demo.py
python tools/benchmark.py --payments 20 --clients 4
```

The test suite has 13 cases, including four multi-process HTTP tests. The demo starts and stops only its own four processes, creates an ephemeral private configuration, pays in 10,000 cents, pays out 3,000, refunds 2,000 and verifies a 5,000-cent wallet and matching reconciliation. Temporary demo databases are removed on exit. `benchmarks/latest.json` records the measured sample, not a capacity guarantee.

For persistent interactive use, generate a private config once:

```powershell
python tools/init_config.py
```

Run each of these in a separate terminal with `PYTHONPATH=src` and this directory as the working directory:

```powershell
python -m payment_system.cli api --config .runtime/private-config.json --port 9870
python -m payment_system.cli provider --config .runtime/private-config.json --port 9871
python -m payment_system.cli receiver --config .runtime/private-config.json --port 9872
python -m payment_system.cli worker --config .runtime/private-config.json
```

Configuration generation refuses to overwrite an existing file. Credentials and databases remain under ignored `.runtime/`; the generator does not modify filesystem permissions. Keep this directory private. The CLI permits only loopback bindings and mock endpoint URLs.

## Merchant API

Send `Authorization: Bearer <merchants.seller token from private-config.json>`. Amounts are positive **integer USD cents**; booleans, floats, other currencies and unknown fields are rejected. A request key is scoped to the merchant across all operation types. Reusing it with changed content returns 409. An order has one pay-in; another key for that order also returns 409.

| Method | Route | JSON body / result |
|---|---|---|
| POST | `/v1/payments` | `{"request_key":"pay1","order_id":"order1","amount":10000,"currency":"USD"}` |
| GET | `/v1/operations/<id>` | Owned operation, status and eventual `checkout` URL |
| POST | `/v1/payouts` | `{"request_key":"out1","amount":3000,"currency":"USD","destination":"test-bank-ok"}` |
| POST | `/v1/refunds` | `{"request_key":"refund1","amount":2000,"currency":"USD","payment_id":"<successful pay-in id>"}` |
| GET | `/v1/wallet` | Available and held cents |
| GET | `/v1/ledger` | Posted transactions with debit/credit lines; 422 above 100 transactions |

A successful POST means **durably accepted**, not paid. Poll the operation, open its PSP-hosted checkout URL and select approve/decline. The mock also accepts `POST <checkout URL>` with `{"choice":"test-success"}` or `test-decline`. Sessions expire after ten minutes; the provider query or checkout submission records expiration as failure. Payout destination `test-bank-decline` exercises rejection. Refund totals include pending/unknown refunds and cannot exceed the original charge; refunds require currently available merchant funds.

Wallets represent merchant liabilities. Pay-in success posts `Dr PSP clearing / Cr merchant available`; payout/refund acceptance reserves `Dr available / Cr held`; success posts `Dr held / Cr PSP clearing`, and definitive failure releases `Dr held / Cr available`. Pending pay-ins credit nothing. Unknown payouts/refunds retain their holds.

## Recovery and administration

Use the distinct admin credential for these routes:

| Method | Route | Purpose |
|---|---|---|
| GET | `/admin/status` | Audit, pending/dead job counts, first 100 open manual reviews |
| POST | `/admin/reconcile` with `{}` | Queue a reconciliation run; returns run ID |
| GET | `/admin/reconciliations/<run>` | `pending`, `done` or `failed`, plus differences |
| POST | `/admin/retry/<operation>` with `{}` | Requeue an unresolved operation with its original PSP identity |
| POST | `/admin/retry-job` | `{"job_id":"notify:<operation>"}`; requeue a dead job without changing event identity |

Workers query before creating at the PSP; only a definitive 404 allows creation. Five consecutive transport failures cause a dead job and manual review. Pending-provider polling does not consume this failure budget. A two-second lease with a unique claim token prevents an expired worker from acknowledging a newer claim; stable PSP IDs and local unique ledger IDs also make overlapping executions harmless. Retry uses the original operation, never a new charge key. Confirm provider state and investigate reviews before manually retrying. Manual reviews are inspection records in this reference, with no resolution UI or automated financial correction.

Terminal operation, ledger, wallet and notification-outbox changes commit together. Webhooks use HMAC-SHA256 over timestamp plus exact request bytes, a five-minute freshness window, durable event deduplication and revision checks. Internal event delivery is at least once, with a durable idempotent consumer; this does **not** claim exactly-once network delivery.

PSP fault injection uses its `/admin/control` route and all four fields:

```json
{"lose_response":true,"webhooks_down":true,"query_down":false,"statement_delta":0}
```

`lose_response` returns 503 after committing create/checkout, simulating an ambiguous response. `query_down` disables queries; `statement_delta` corrupts one successful statement row only. Receiver `/admin/control` accepts `{"lose_ack":true}` to commit an event before returning 503. Reset flags to recover. These are local test controls, not real provider features.

## Evidence and limits

See [design](docs/design.md) and [measured verification](benchmarks/REPORT.md). A 20-payment/4-client Windows sample completed at **4.75 pay-ins/s**, end-to-end p95 **970.59 ms**. This does not meet or establish the book's roughly 10 TPS target.

The payment authority is one SQLite WAL database, with serialized writes, not a replicated financial database. Multiple processes validate networking and restart behavior, not regional availability or automatic failover. No real PSP, PCI certification, bank settlement, FX, fees, tax, chargebacks, multi-order checkout, merchant onboarding, distributed transactions, production monitoring or production authorization lifecycle is implemented. The provider is capped at 10,000 operations, reconciliation reads a full bounded provider snapshot and the ledger HTTP view is limited to 100 transactions. Large histories require pagination/streaming and a stronger operational design.
