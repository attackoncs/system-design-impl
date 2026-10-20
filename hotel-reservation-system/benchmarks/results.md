# Validation and measurements

Date: 2026-10-20. Python 3.13.9, Windows 11 build 26100. All payment actions below are simulated local ledger operations. Raw benchmark output: [results.json](results.json).

## Test evidence

```powershell
New-Item -ItemType Directory -Force .runtime/tmp | Out-Null
$env:TEMP = (Resolve-Path .runtime/tmp).Path
$env:TMP = $env:TEMP
python -m unittest discover -s tests -v
```

**13 tests passed in 18.723 seconds:** nine core transactional/state-machine tests and four real HTTP integration tests. Each network lab uses six subprocesses: gateway, two shard servers, two saga workers and a separate simulated payment service.

| Area | Verified behavior |
| --- | --- |
| Multi-night atomicity | A full second night rolls back the first night's increment, order, job and event; missing calendars also fail |
| Real pre-commit process exit | A separate child calls `os._exit(74)` inside the reservation's pre-commit hook; reopening via the HTTP shard shows zero orders and zero reserved units |
| Price and idempotency | Same key returns the original order and frozen amount after rate changes; changed content fails; new reservations must accept the updated total |
| Concurrent inventory | 30 concurrent unit attempts against capacity nine admit exactly nine; direct SQL beyond the CHECK constraint fails |
| Real concurrent clients | 40 HTTP requests against ten physical rooms admit exactly eleven; both nights remain eleven and a hotel on the other shard books independently |
| Rounding and policy changes | Nine at 10% stays nine; ten permits eleven; lowering policy/capacity below existing occupation is rejected atomically |
| Cancellation and expiry | Repeated cancellation releases once; never-attempted canceled/expired charge is skipped; late charge success cannot confirm an expired order |
| Unknown payment outcome | Simulator commits a charge then returns 503; retry uses the same provider operation, with one unique charge |
| Durable refund compensation | Cancel during unknown charge, restart worker/payment service/shard, temporarily fail refunds, then recover to exactly one refund and restored inventory |
| Lease fencing and receipts | Old worker token cannot settle a reclaimed job; wrong amount/provider receipt and reused payment key with altered payload fail |
| ACL and catalog | Other guests cannot inspect an order; guest cannot administer hotels; soft-deleted room types reject new bookings but retain cancellation access |
| Cache and journal | Independent Store instance changes persisted inventory revision; cached availability refreshes; state transitions remain in durable paginated events |

Unit rollback hooks are injected exceptions. The child exit, shard/payment restart and worker kill/restart are actual separate-process faults on retained disks. Lost acknowledgement and refund outage are explicit simulator controls; no real provider, banking network, replicated database, disk failure or cross-machine test is claimed.

## Demo

```powershell
python examples/network_demo.py
```

Observed six processes. Repeated booking returned the same ID. Two nights cost 20,000 cents; pending hold became confirmed/paid in **216.24 ms** in one observation. Cancellation completed a simulated refund, with exactly two provider operations (one charge, one refund), and availability returned to eleven units. This is a single localhost observation, not an SLA or a percentile.

## Hot-hotel contention benchmark

```powershell
python tools/benchmark.py --output benchmarks/results.json
```

| Measurement | Observed |
| --- | ---: |
| Processes / configured hotel shards | 6 / 2 |
| HTTP attempts / clients | 100 / 16 |
| Physical rooms / overbooking | 20 / 10% |
| Stay nights | 2 |
| Accepted unique bookings | 22 |
| Capacity conflicts (409) | 78 |
| Reserved units on each night | 22 / 22 |
| Unique simulated charges | 22 |
| Admission wall time | 1.185 s |
| All accepted bookings confirmed/paid | 2.037 s |
| Attempt requests per second | 84.38 |
| Successful bookings per second through settlement | 10.80 |
| All-request p50 / p95 | 41.731 / 779.692 ms |
| SQLite file bytes at measurement | 188,416 |

All requests go through real HTTP gateway and shard transactions while workers charge through the separate mock ledger. Assertions check exact capacity on both nights, all accepted orders confirmed and one unique charge each. All-request latency includes both accepted requests and expected capacity rejections. It must not be reported as 84 successful bookings/s: only 22 of 100 requests succeeded, with settlement throughput 10.80/s.

This is a short burst against one hot hotel on one SQLite shard; the second configured shard is idle in this benchmark (its independent operation is covered by the correctness test). Measurements do not establish a scalable 5000-hotel cluster, sustained performance, payment-provider latency or production availability. SQLite writer serialization, plaintext localhost HTTP, no replication, static placement, no privacy/receipt retention and the simulated payment provider remain explicit boundaries.
