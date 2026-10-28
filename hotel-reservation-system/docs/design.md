# Hotel reservation design — Chapter 23

Core review: 2026-10-26. [Chapter mapping](chapter23-review.md) and
[executed evidence](../benchmarks/results.md) distinguish implemented behavior
from the book's production targets. Standard library, Python 3.10+.

## Boundaries and routing

The lab has six independent processes: gateway, two shard HTTP servers, two saga
workers, and one simulated payment ledger. Gateway/workers use HTTP and do not
open shard files. Each hotel's catalog, room types, daily inventory, orders,
nightly snapshots, jobs and events live in one SQLite WAL shard. Routing is the
first 64 bits of SHA256(canonical JSON hotel ID) modulo the fixed shard count;
stored topology rejects an incompatible reopen. No online resharding or replica
failover is implemented. Physical room-number assignment and hotel search remain
outside scope. All payment behavior is explicitly simulated.

## Inventory and prices

A room type, not an individual room, is booked. Inventory is keyed by
(hotel, room type, UTC day). Limit is capacity + floor(capacity*overbook/100),
with overbook 0..10; pending holds and confirmed orders both consume units.
`BEGIN IMMEDIATE`, conditional increments and CHECK constraints protect the
multi-night update. Missing/full nights, changed accepted totals or process exit
before commit roll back inventory, order, price snapshots and charge intent
atomically. SQLite has one writer per shard, not row-level locks. Version metadata
and read snapshots are not a claim of an optimistic-lock retry implementation.

Amounts are bounded integer USD cents. `[checkin, checkout)` has 1..30 nights,
quantity 1..10 and at most 730 future UTC days. Quotes are advisory; expected_total
is checked again in the writer. Accepted nightly prices remain immutable. Existing
occupancy prevents administrators lowering a day's capacity/policy below usage.
Catalog soft-delete blocks new reservations while keeping historical refunds.

## Read consistency and rolling calendars

Catalog and availability use isolated TTL/LRU results and fixed-stripe cold-load
coalescing. Cache keys include durable revision; loaders read revision, active
catalog and inventory in one SQLite snapshot. Separate saga workers invalidate
revision when inventory changes. The database remains authoritative on every
booking. Hotel-wide availability scans at most 100 active types and 30 nights;
it returns only complete ranges with enough quantity and reports its revision.

CalendarPolicies is opt-in. Admin PUT requires capacity/overbook/price, with
horizon_days 1..730 (default 730). The shard maintenance worker defaults to five
seconds and a pass of at most 32 policies/4096 examined days. It captures rows
outside the writer, then checks the current UTC day plus persistent generation,
version and cursor before publishing progress. A non-reused generation prevents
delete/recreate ABA. Identical configuration PUT keeps progress. Only missing
dates are inserted: existing prices, reservations and historical rows are never
repriced or deleted. Shrinking the horizon or disabling a policy preserves old
inventory. Inactive catalog entries do not receive new rows. The internal bounded
roll endpoint uses the same implementation and is not publicly proxied.

## Identity and idempotency

Guest identity comes from configured Bearer credentials, never a body user field.
Reservation identity is hotel + '.' + the first 128 digest bits of [guest,key],
with UNIQUE(hotel,user,request_key) and an immutable body fingerprint. Exact replay
returns the current order even after cancellation/expiry, date movement or rate
changes; it never reacquires rooms. Changed payload conflicts. Missing and foreign
orders both return 404. History is hotel-scoped, ID-cursor paginated to 100 items;
it is not a cross-shard guest index or a snapshot across concurrent insertions.
Admins manage this lab globally; per-hotel staff ACLs are not implemented.

## Saga and durable operations

PENDING is an inventory hold. Only full charge success before its deadline yields
CONFIRMED/PAID. An explicit decline releases the hold and rejects it; a timeout or
HTTP failure means unknown, never decline. Cancel/sweep/finish serialize the held
flag, releasing units once. A success after cancellation or expiry creates a
compensating refund instead of confirming. Confirmed orders do not expire as
pending holds. Cancellation is full and penalty-free in this reference.

Charge/refund jobs use stable IDs and frozen intent. Claim persists an attempt
and lease token; retry/finish validate durable identity and fence a replaced owner.
Network operations run outside inventory transactions. Unknown outcomes stay
pending for idempotent resolution rather than being silently discarded. Sweep is
bounded to 100 pending holds per pass, so downtime can conservatively reserve
units until work resumes. Independent workers own their stop/cancel/drain state;
legacy custom adapters without cancellation support still require real work to
exit. Provider receipts, amounts and simulated status are validated before settle.

The simulator's SQLite operations freeze their idempotent fingerprints and
receipts. Refund requires the original matching paid charge. Test controls can
return 503 after a durable operation or temporarily reject refunds. Retrying a
completed operation still returns its original result; an outage flag must not
undo the idempotency contract. This is not a bank, real-card SDK or exactly-once
external-money guarantee. Events are a durable polling journal, not a guaranteed
external notification bus; financial/receipt history still needs production GC.

## Transport and process ownership

HTTP defaults: 32 active handler slots, 128 backlog, ten-second absolute socket
I/O budget, 1MiB requests and 4MiB responses. Explicit method/path/query contracts
are shared by gateway and shard; auth precedes body reads. Gateway forwards only
public routes with the original identity and never blindly retries writes.
Strict JSON rejects duplicate/non-finite values; ambiguous framing, unknown
queries and unsupported bodies are rejected. Direct RPC does not inherit proxies
or follow redirects; HTTPS uses normal certificate checks. Overload rejection
has bounded drain so unread TCP data does not overwrite 503 with a Windows RST.

CLI config is bounded to 1MiB, validates distinct credentials, origin URLs, topology
and durations. Shard maintenance starts only after bind. Shutdown stops/drains
handlers and then owned maintenance; errors are sanitized and visible through
health/status with retries. Socket deadlines cannot terminate accepted DNS,
SQLite or CPU work; capacity remains occupied until that work really exits.

## Remaining production work

No replicated shard/ledger, consensus metadata, automatic failover, live topology
migration, actual Redis/CDC, distributed throttling, real payment integration,
TLS/tenant ACL deployment, hotel-local timezone/tax/currency policies, privacy
retention, backup/disaster recovery or sustained 5000-hotel/million-room validation
is claimed. Retained-disk process tests do not prove machine loss or universal
Windows filesystem power-loss durability.
