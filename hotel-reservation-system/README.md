# Hotel Reservation System — Chapter 23

<!-- LEARNING_GUIDE_START -->
## Code reading guide

[Learn through a concrete example: architecture, state changes and annotated source (Chinese)](docs/learning-guide_cn.md).
<!-- LEARNING_GUIDE_END -->

A Python standard-library reference with a real six-process HTTP lab: gateway, two hotel shards, two durable saga workers and a separate **simulated** payment ledger. Includes hotel/room-type management, nightly prices and inventory, atomic multi-night reservations, up to 10% overbooking, idempotent booking/cancellation, expired holds, refunds and recovery after unknown payment outcomes.

Python 3.10+; no external packages or services required. No real money is charged. Hotel search and physical room-number assignment are outside scope.

The 2026-10-26 core review adds hotel-wide room-type availability and rolling
calendar defaults, separates storage/saga/transport responsibilities, validates
durable job identities and receipts, and bounds HTTP/worker work. Current results
and remaining book gaps: [validation](benchmarks/results.md),
[chapter mapping](docs/chapter23-review.md). Historical 2026-10-20 figures retain
their dates and are not the final review result.

## Run

From this directory:

```powershell
$env:PYTHONPATH = 'src'
python examples/network_demo.py
python -m unittest discover -s tests -v
python tools/benchmark.py --output benchmarks/results.json
```

The lab chooses localhost ports, generates private credentials, launches six independent processes and stops only those it owns. Ignored `.runtime/lab-*` retains data/logs for inspection. If system temporary-directory access is restricted, create `.runtime/tmp` and set `TEMP`/`TMP` to its absolute path before running tests. See [validation report](benchmarks/results.md).

## Manual deployment

```powershell
$env:PYTHONPATH = 'src'
python tools/init_config.py
# Run each command in a separate terminal, with the same PYTHONPATH:
python -m hotel_reservation.cli gateway --config .runtime/secrets.json --port 9830
python -m hotel_reservation.cli payment --config .runtime/secrets.json --port 9831
python -m hotel_reservation.cli shard --config .runtime/secrets.json --shard 0 --port 9832
python -m hotel_reservation.cli shard --config .runtime/secrets.json --shard 1 --port 9833
python -m hotel_reservation.cli worker --config .runtime/secrets.json --shard 0
python -m hotel_reservation.cli worker --config .runtime/secrets.json --shard 1
```

The generated private config has distinct `admin_token`, `internal_token`, `payment_token`, guest `users` credentials for alice/bob, ordered `shard_urls`, `payment_url`, `hold_seconds=120` and `lease_seconds=2`. Creation refuses overwrite. Tokens and databases stay under ignored `.runtime`; do not publish them. Services default to `127.0.0.1` and only simulate payments.

## Catalog, calendar and booking

Use bearer authorization. Guest tokens access catalog/availability and their own reservations; admin tokens manage catalog/calendar and can inspect/cancel reservations. The gateway routes by hotel ID.

| Method and route | Access and behavior |
| --- | --- |
| `POST /v1/hotels` | Admin: `{id,name,address}` |
| `GET/PUT/DELETE /v1/hotels/{hotel}` | Details; admin PUT `{name,address}` or soft-delete |
| `POST /v1/hotels/{hotel}/rooms` | Admin: `{id,name,description}` |
| `GET/PUT/DELETE /v1/hotels/{hotel}/rooms/{type}` | Room-type details; admin update/soft-delete |
| `PUT /v1/hotels/{hotel}/rooms/{type}/calendar` | Admin: `{days:[{day,capacity,overbook,price},...]}` |
| `GET /v1/hotels/{hotel}/rooms/{type}/availability?start=YYYY-MM-DD&end=YYYY-MM-DD` | Advisory nightly prices/units and `unit_total` |
| `GET /v1/hotels/{hotel}/availability?start=YYYY-MM-DD&end=YYYY-MM-DD&quantity=1` | Active room types with a complete calendar and enough units for every night; bounded to 100 source types |
| `GET/PUT/DELETE /v1/hotels/{hotel}/rooms/{type}/calendar-policy` | Admin: inspect, set defaults, or disable future automatic fill |
| `POST /v1/reservations` | Guest: acquire all-night hold and persist payment intent |
| `GET /v1/reservations?hotel={hotel}&after={cursor}` | Guest history, at most 100 items; optional cursor |
| `GET/DELETE /v1/reservations/{id}` | Owner or admin: inspect/cancel; cancel is idempotent |
| `GET /health` | Minimal public role status |

PUT catalog calls create or update; DELETE retains historical references and blocks new bookings. Hotel/type IDs use 1–80 ASCII letters, digits, `_` or `-`. Rooms are booked by type; an actual room number is not allocated. Calendar capacity is physical saleable units, `overbook` is an integer from 0 to 10, and `price` is USD cents for one room on that night. Populate every night in a stay; incomplete calendars reject the entire booking.

```python
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from hotel_reservation.common import http_json
c = json.loads(Path('.runtime/secrets.json').read_text())
url = 'http://127.0.0.1:9830'
hotel, room = 'demo', 'king'
first = datetime.now(timezone.utc).date() + timedelta(days=10)
start, end = first.isoformat(), (first + timedelta(days=2)).isoformat()
http_json(url + '/v1/hotels/demo', c['admin_token'],
          {'name': 'Demo Hotel', 'address': 'Demo address'}, 'PUT')
http_json(url + '/v1/hotels/demo/rooms/king', c['admin_token'],
          {'name': 'King', 'description': 'King room type'}, 'PUT')
http_json(url + '/v1/hotels/demo/rooms/king/calendar', c['admin_token'], {'days': [
    {'day': (first + timedelta(days=i)).isoformat(), 'capacity': 10, 'overbook': 10, 'price': 10000}
    for i in range(2)]}, 'PUT')
order = http_json(url + '/v1/reservations', c['users']['alice'], {
    'hotel': hotel, 'room': room, 'start': start, 'end': end, 'quantity': 1,
    'request_key': 'booking-001', 'expected_total': 20000, 'payment_method': 'test-ok'})
print(order)  # PENDING; poll GET /v1/reservations/{id} for CONFIRMED/PAID.
```

Only `test-ok` and `test-decline` are accepted payment methods. Do not send card details. Date ranges are `[checkin,checkout)`, 1–30 nights, at most 10 rooms, within the next 730 UTC calendar days. The expected total is checked against current nightly prices inside the inventory transaction; a changed total returns 409 and requires accepting a new quote. Accepted nightly prices remain frozen even when calendar rates change.

For opt-in rolling calendars, PUT a policy such as
`{"capacity":10,"overbook":10,"price":10000,"horizon_days":730}`. Capacity,
overbook and price are required; horizon is 1..730 days, default 730. The shard's
owned maintenance worker runs every `calendar_interval` seconds (default 5),
examining at most 32 policies/4096 day rows per pass. It inserts **missing dates
only**, with durable progress/version checks; explicit daily prices, occupancy
and old rows remain intact. Updates affect future missing dates, not bulk
repricing. DELETE disables the policy and preserves existing inventory. Inactive
catalog entries are not filled. `POST /internal/calendar-roll` with `{}` permits
an internal/admin bounded pass through the shard, never through the gateway.

## Correctness and recovery

- Sellable units are `capacity + floor(capacity*overbook/100)`. At 10%, nine rooms permit nine bookings, ten permit eleven. Pending and confirmed orders both occupy inventory. Every night's update, the order, snapshots and charge job commit atomically; any missing/full night rolls back all nights.
- Idempotency is scoped to `(hotel,guest,request_key)`. Same content returns the same order's current state; changed content returns 409. A canceled or expired order cannot be revived with its old key. Separate guest identities cannot read or cancel each other's orders.
- PENDING is a temporary hold, not confirmed accommodation. Full simulated charge success before expiry makes CONFIRMED/PAID. Explicit decline rejects/releases; ambiguous errors keep a durable retry. Workers release expired pending holds; during worker downtime availability can remain conservatively reduced until recovery.
- Cancellation releases units once. Paid orders enqueue full refunds. If an unknown in-flight charge succeeds after cancellation/expiry, settlement enqueues compensation. Payment/refund operations use fixed IDs and frozen receipts, and lease tokens fence stale workers. No booking confirms after its hold deadline.
- Confirmed bookings do not expire as holds. This reference permits full cancellation/refund without penalties; cancellation policies, taxes and hotel-local timezones are not implemented.
- Hotel sharding keeps each reservation in one SQL transaction. SQLite `BEGIN IMMEDIATE`, conditional increments and CHECK constraints protect writes. There are no row-level SQLite locks or implemented optimistic retry algorithm.
- Catalog/availability use bounded one-second caches keyed by persisted revision, so separate worker inventory changes invalidate reads. Availability remains advisory; booking never trusts it for authorization or capacity.

Cache loads now coalesce on fixed stripes and return isolated values. Catalog and
availability loaders read revision/catalog/inventory in a consistent snapshot.
Booking payloads and job intents are captured before writer waits; transaction
clocks are sampled after acquiring the writer. Queue-ready/sweep/history indexes
keep bounded maintenance off unrelated history scans. Same-shard SQLite writes
still serialize; this review does not claim row locks or an optimistic retry mode.

HTTP defaults are 32 admitted handler threads, 128 backlog, a 10-second absolute
socket I/O deadline, 1MiB request and 4MiB response limits. CLI options
`--concurrency` (1..256) and `--request-timeout` (>0..3600 seconds) tune them.
The gateway validates an explicit route/method/query contract, authenticates
before reading bodies, forwards the original identity once, and never blindly
retries a write. RPC ignores environment proxies and redirects, validates framing
and JSON, and uses normal HTTPS certificate verification. Deadlines cannot kill
accepted DNS/SQL/CPU work; capacity and shutdown wait for real work to exit.
Maintenance starts after a successful HTTP bind, reports sanitized errors through
health/status, retries, and is drained on exit. Payment workers likewise own their
non-daemon lifetime and use a per-round RPC deadline/cancellation boundary.

On shard servers, internal/admin credentials can access `GET /internal/status`
(unfinished jobs, errors, expired holds and maintenance errors),
`GET /internal/events?after=0` (durable paginated event journal), and POST
`/internal/sweep`, `/internal/claim`, `/internal/finish`, `/internal/retry`,
`/internal/calendar-roll`. These routes are not proxied by the gateway.

The separate payment simulator provides payment/admin-only `POST /operations` and admin-only `GET /admin/status` / `POST /admin/control`. The control body is `{"fail_after_commit":true,"fail_refunds":false}` to emulate a durable charge with missing acknowledgement; setting refund failure tests compensation retries. Defaults are both false. These are simulation controls, not real financial endpoints.

## Scope limits

SQLite shards and simulator have no replication or automatic failover. Restart
tests retain disk; host/disk loss is untested. Fixed routing does not support
online shard-count changes. Catalog, rates and orders are colocated rather than
separate transactional databases. Search, real payment integration, a global
cross-shard guest index, tenant staff ACLs, TLS, distributed rate limiting,
automatic privacy retention, hotel-local timezones/taxes and the book's
5000-hotel/million-room capacity remain extensions. See [design](docs/design.md).
