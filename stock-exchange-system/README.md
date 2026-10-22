# Stock Exchange — Chapter 29

A standard-library Python reference for deterministic limit-order matching, cancellations, reserved cash/shares, price-time priority, asynchronous matching replicas and recoverable market data. **Accounts, holdings, clock and all trades are simulated. No orders reach a real exchange.**

Four real loopback HTTP processes run independently: gateway/sequencer, primary matching engine, backup matching engine, and market-data/report projection. Each uses its own SQLite database. The sequencer is a single authority; matching failover is manual and does not provide sequencer HA.

## Run

Python 3.10+, no third-party runtime dependencies. From this directory in PowerShell:

```powershell
$env:PYTHONPATH = 'src'
New-Item -ItemType Directory -Force .runtime/tmp | Out-Null
$env:TEMP = (Resolve-Path .runtime/tmp).Path
$env:TMP = $env:TEMP
python -m unittest discover -s tests -v
python examples/network_demo.py
python tools/benchmark.py --pairs 20 --clients 4
```

13 tests include four actual HTTP multi-process tests. The demo matches two FIFO sells against one buy, refunds price improvement, stops the primary, promotes the caught-up backup and cancels the remaining sell quantity. It verifies cash/share conservation, reporting and candles. Temporary labs stop only their own processes and remove their databases on exit.

For persistent use, run `python tools/init_config.py` once, then launch four terminals with `PYTHONPATH=src` and this directory as working directory:

```powershell
python -m stock_exchange.cli gateway --config .runtime/private-config.json
python -m stock_exchange.cli primary --config .runtime/private-config.json
python -m stock_exchange.cli backup --config .runtime/private-config.json
python -m stock_exchange.cli market --config .runtime/private-config.json
```

Default ports are 9890–9893 respectively. Configuration generation refuses overwrite; `.runtime/` is Git-ignored. Keep generated role credentials private; the generator does not modify filesystem permissions. Only loopback HTTP endpoints are supported.

The generated lab has Alice, Bob and Carol, each with 1,000,000 synthetic USD cents and 1,000 shares each of AAPL/MSFT. Its clock starts at **2026-10-21 14:00 UTC**, with simulated weekday trading 09:30–16:00 **UTC**. This is not the NYSE calendar/timezone. Clock advances only through the administrator endpoint. There is no real-time clock feed, holiday calendar or DST handling.

## Trading API

Use `Authorization: Bearer <owners.alice token from private-config.json>`. Distinct credentials protect other owners, administrators and internal services.

`POST /v1/orders`:

```json
{"request_id":"request_bid1","order_id":"bid1","symbol":"AAPL","side":"BUY","price":110,"quantity":8}
```

Price is positive **integer USD cents** with a one-cent tick, and quantity is positive integer shares. Floats, booleans, extra fields, market orders and short sales are unsupported. Accepted new quantity per owner/symbol/UTC date is capped at the configured daily limit (default 1,000,000). Canceled quantity still counts toward this conservative submission limit. The identity, session and risk abstractions are in `http.py` and `matching.py`.

The response assigns a durable sequencer number and means **accepted for processing**. Poll `GET /v1/requests/<request_id>` for the matching result: `accepted=true` plus status, or `accepted=false` plus a business rejection reason. Processing can finish while a caller loses the response. Always reuse the original request ID after uncertainty; the same owner/key/content returns the same sequence, changed content returns 409. Order IDs are globally unique in the reference.

| Method | Route | Purpose |
|---|---|---|
| POST | `/v1/cancels` | `{"request_id":"cancel1","order_id":"bid1"}` |
| GET | `/v1/orders/<order_id>` | Owned order with filled/remaining quantities and status |
| GET | `/v1/account` | Available/held cash and positions on the active engine, with applied sequence |
| GET | `/v1/requests/<request_id>` | Owned command result or PENDING |

Matching uses best opposing price, then earliest sequencer number, and executes at the resting order's price. Buyer cash is reserved at its limit; price improvement is immediately released. Sell orders reserve shares. Partial fills retain the exact remaining reservation. Cancellation releases it. Self-trade prevention cancels the incoming remainder when it reaches its owner's resting order; earlier legitimate partial fills remain. Cancel/fill races resolve by the sequencer order, not client timestamps.

Orders are DAY orders. At the next sequenced command on a new UTC date, prior-day open orders expire and release reservations. An idle book is not swept just because the simulation clock advanced. Cancellations are permitted outside the new-order trading session.

## Market data and private reports

Use port 9893 (market service). Public market output contains no owner identities or order IDs.

| Method | Route | Result |
|---|---|---|
| GET | `/market/snapshot` | All L2 books with global sequence and chain head |
| GET | `/market/deltas?after=<seq>` | Ordered level updates and public trades; eight retained batches |
| GET | `/market/book?symbol=AAPL&depth=10` | Sorted L2 depth; depth 1 gives best bid/ask |
| GET | `/market/candles?symbol=AAPL&resolution=60` | Trade-based OHLCV buckets; resolution 1–86400 seconds |
| GET | `/v1/report` with owner bearer token | Owned accepted orders and fills, one stable fill ID per trade side |

Delta quantities are **absolute level sizes**, with zero deleting a level. Start from a snapshot, then require consecutive sequence numbers and matching `previous`/head hashes. HTTP 410 means the delta window was missed: fetch a new snapshot. The market process uses the same gap/resnapshot protocol against the engine. Snapshot replacement and batch application commit atomically with the projection cursor. Repeated batches do not duplicate fills/trades.

Reports and market views are asynchronous and include their applied sequence. Polling provides updates; there is no FIX, multicast or WebSocket streaming. Candles include only buckets with trades. Rejected commands remain in the sequenced audit journal and request-result API; the private report lists accepted orders/fills.

## Replication, recovery and administration

Both matching processes independently replay the immutable sequencer command prefix into their own account/book projections and output batches. Results, trade IDs and fill IDs are deterministic. Symbol books are logically separate but share one serialized engine and account ledger; this is not parallel per-symbol process sharding. New commands are durable before either matching replica applies them. Backup lag does not block order acceptance or active-engine results.

Use the admin credential:

| Service | Route | Purpose |
|---|---|---|
| Gateway | `GET /admin/status` | Durable epoch, active matching node, simulation clock and journal tail |
| Gateway | `POST /admin/promote` with `{"node":"backup"}` | Select only a node caught up to the exact journal tail; increment epoch |
| Gateway | `POST /admin/clock` with `{"timestamp":1792591260}` | Advance the simulation clock; cannot move backward |
| Engine | `POST /admin/snapshot` with `{}` | Checksum snapshot of current replay state |
| Engine | `GET /admin/audit` | Cash/share supply, reservation checks and independent replay comparison |
| Market | `GET /admin/status` | Applied sequence/head and snapshot resync count |

After primary failure, keep the sequencer available, allow backup replay to catch up, then promote manually. A concurrent new command can cause promotion to return 409; retry after catch-up. Old engines cannot publish through `/internal/serve` or feed after the active epoch changes. The inactive node may continue computing the same command prefix as a standby; these are replicated projections, not additional external trades. A restarted gateway retains the selected node and epoch. Engine publication fails closed when the sequencer/control service is unavailable.

Snapshots are fsynced and atomically replaced. Startup checks the entire command hash chain and validates snapshot state against its replay prefix before applying tail commands. Replay never resubmits a client order or sends a trade externally. This prioritizes validation over startup speed; logs are retained, not compacted. Raw administrative/internal replica reads are distinct from active publication.

## Measured limits

See [design](docs/design.md), [verification report](benchmarks/REPORT.md) and [raw sample](benchmarks/latest.json). A 20-taker/4-client sample measured **14.43 filled taker orders/s**, submit-to-fill **p99 473.58 ms**. It includes HTTP and client polling, not a standalone matching-engine latency measurement; with 20 observations, nearest-rank p99 is the maximum sample.

The reference has no sequencer replication, automatic failover, independent-host durability, production KYC/compliance, trading calendar, settlement/clearing, real funds, shorting, fees, market orders or FIX. It bounds configuration to 100 symbols/100 accounts and history to 1,000 commands, uses full JSON state copies and linear book scans, and retains all command history. The book's billion orders/day, low-millisecond tail latency and 99.99% availability are unproven targets.
