# Validation and measurements

## Core review — 2026-10-26 (Asia/Shanghai)

Python 3.13.9 / Windows 11 build 26100, standard library only. All payments are
simulated operations in an owned local ledger; no real funds/provider was used.
Actual localhost fixtures ran in approved unrestricted processes because the
Windows sandbox blocks these sockets. Temporary data stayed in the workspace;
owned services were stopped and private diagnostics remain ignored in `.runtime/`.

### Final regression

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -q
```

**99 tests passed in 22.901s, no skips and no ResourceWarning.** The pre-change
baseline passed 13 tests in 18.699s. Final coverage:

| Area | Tests |
| --- | ---: |
| Original inventory/state machine and independent-process HTTP faults | 13 |
| Store, input ownership, snapshot cache, time and saga races | 20 |
| Simulated payment ledger/intent/receipt | 14 |
| Owned saga worker, cancellation and unknown outcomes | 12 |
| Actual HTTP/RPC framing, authorization, overload and shutdown | 17 |
| CLI and periodic maintenance | 7 |
| Rolling calendar and hotel-wide availability | 16 |

Checks include frozen requests before writer waits, one transaction clock sample,
cross-night rollback, cache revision/content snapshot consistency, cold-load
coalescing, strict numeric receipt types, durable intent/method matching, expired
first-attempt charge suppression, missing-night release failure and concurrent
cancel/settle with one refund. Simulator lost-ACK/refund outages and controlled
provider failures are labeled test faults; separate actual process/network tests
continue to cover retained-disk crash/restart and real HTTP boundaries.

Rolling tests verify bounded restart cursors, manual price/held-stock preservation,
UTC-day changes, concurrent rollers, unchanged PUT progress, soft deletion and
hotel-wide quantity/snapshot queries. Independent review found delete/recreate
could reuse version/cursor values: a non-reused generation and an explicit ABA
regression now prevent an old roller publishing stale policy defaults.

Ruff E9/F, formatting and Python 3.10 syntax were checked for **31 Python files**;
local documentation links and `git diff --check` were also checked.

### Actual six-process examples

```powershell
python examples/network_demo.py
python examples/chapter_demo.py --output benchmarks/features-2026-10-26.json
```

The original gateway/two-shard/two-worker/simulated-payment demo passed. Identical
booking retry returned the same ID; two nights cost 20000 cents and became
CONFIRMED in **183.85ms** in this observation. Cancellation/refund completed with
two unique operations (one charge, one refund), restoring availability to 11.

The [new feature report](features-2026-10-26.json) records actual HTTP/CLI behavior:

- Background maintenance filled a 730-day horizon for each of two room types,
  without calling the manual roll endpoint to trigger initial population.
- Policy administration rejected a guest; identical PUT retained its version.
- Hotel-wide lookup found king/queen, while quantity=2 selected only king.
- A manually priced 12000-cent night survived default fill; two rooms/two nights
  cost 44000 cents, with unchanged booking ID, total and nightly snapshots on retry
  or subsequent policy changes.
- Simulated refund restored availability to 11; disabling a policy retained all
  existing inventory. No live payment service was contacted.

### Current hot-hotel contention measurement

```powershell
python tools/benchmark.py --output benchmarks/review-2026-10-26.json
```

Final measurement ran after the regression/demo, without another suite or graph
refresh running concurrently. [Raw report](review-2026-10-26.json):

| Measurement | Observed |
| --- | ---: |
| Processes / hotel shards | 6 / 2 |
| HTTP attempts / clients | 100 / 16 |
| Physical rooms / overbooking / nights | 20 / 10% / 2 |
| Accepted bookings / expected capacity conflicts | 22 / 78 |
| Reserved units per night | 22 / 22 |
| Unique simulated charges | 22 |
| Admission / all settled duration | 1.287 / 2.543s |
| Attempt requests per second | 77.7 |
| Successful bookings per second through settlement | 8.65 |
| All-request p50 / p95 | 36.196 / 666.458ms |
| Approximate SQLite physical bytes sampled | 245760 |

All accepted orders were independently read back as CONFIRMED. All-request latency
includes the 78 expected capacity rejections; **77.7 attempts/s is not successful
booking throughput**. The second shard is configured but idle in this hot-hotel
measurement; independent hotels across shards are covered by correctness tests.
The first measurement attempt completed domain assertions but failed while an
ephemeral SQLite SHM file disappeared during file-size sampling. The measurement
helper now tolerates disappearing sidecars and the final run above completed;
the byte sample remains approximate, not an atomic filesystem snapshot.

This is a short burst with warm local disks, not a controlled speedup ratio,
sustained multi-host capacity, real payment latency, availability SLA or power-cut
test. Historical Oct20 data below retains its original date and workload caveats.

### Graph evidence and remaining limits

code-review-graph minimal context, incremental refresh, caller and impact queries
completed. The final refresh updated 21 files without build errors. Six existing
core files reported 72 directly changed nodes, 16 impacted nodes, four additional
files and 2122 unresolved call sites, with truncated edges. SimulatedLedger.execute
had five bound call sites across two test nodes. Dynamic HTTP dispatch can omit
edges, and the new untracked `calendar_policy.py` was explicitly not indexed.
Source review, independent review and executed behavior supplement the graph;
other chapters' existing uncommitted work remains in repository-wide context.
See [chapter mapping](../docs/chapter23-review.md).

SQLite still has one writer per shard, static topology and no database/ledger
replication or automatic failover. Cache is local and entry-bounded, not Redis/CDC.
Calendar policies default to 730 UTC days, 32 policies/4096 examined days per pass;
their generation/cursor protocol only adds missing dates and does not reprice or
remove existing inventory. Schema/receipts/events need future retention work.
Worker defaults to a 10s RPC round; HTTP defaults to 32 handlers/128 backlog/10s,
1MiB requests and 4MiB responses. DNS, SQLite and arbitrary adapter CPU/I/O cannot
be killed by a socket deadline; shutdown drains accepted work. Real payments,
hotel-local calendars, fees/currency/policies, staff ACLs, complete UI, TLS/limits,
HA/resharding and the book's 5000-hotel scale remain explicit production extensions.

## Historical verification — 2026-10-20

Date: 2026-10-20. Python 3.13.9, Windows 11 build 26100. All payment actions below are simulated local ledger operations. Raw benchmark output: [results.json](results.json).

### Test evidence

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

### Demo

```powershell
python examples/network_demo.py
```

Observed six processes. Repeated booking returned the same ID. Two nights cost 20,000 cents; pending hold became confirmed/paid in **216.24 ms** in one observation. Cancellation completed a simulated refund, with exactly two provider operations (one charge, one refund), and availability returned to eleven units. This is a single localhost observation, not an SLA or a percentile.

### Hot-hotel contention benchmark

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
