# Hotel reservation reference design

## Service boundaries and storage

The lab runs six independent processes: HTTP gateway, two hotel shard HTTP servers, two saga worker processes, and a durable **simulated** payment HTTP service. The gateway and workers do not open shard databases; all production-path communication crosses HTTP. Each shard owns a separate SQLite WAL file, and the payment simulator owns another. Only fault tests open a shard file from a separate child to inject a pre-commit process exit or inspect unit invariants.

Routing is `first_64_bits(SHA256(canonical_JSON(hotel_id))) % shard_count`. A hotel's catalog, room types, date inventory, orders, nightly snapshots and payment outbox are colocated. Multi-night reservations therefore require no cross-shard distributed transaction. Shard ID/count is persisted and checked on startup, and wrong-owner writes fail. Gateway configuration must stay consistent across deployment; online resharding is not implemented. Changing topology requires an explicit offline migration rather than changing a running shard count.

Room types, not specific physical room numbers, are reserved. Hotel and room deletion is a soft active flag that prevents new bookings while retaining inventory, orders and cancellation/refund access. Reactivation is an explicit administrative PUT. Daily calendar rows are populated by administrators; missing nights are rejected instead of inferred. There is no daily two-year horizon-population scheduler in this reference.

## Inventory and price invariants

Inventory primary key is `(hotel, room_type, UTC_calendar_day)` with capacity, overbook percentage, price cents, reserved units and version. Reserved includes pending payment holds and confirmed reservations. The SQL constraint is:

```sql
CHECK(reserved >= 0 AND reserved <= capacity + (capacity * overbook) / 100)
```

All operands are integers and inputs bound capacity to 100000, so division floors the additional allowance. Nine physical rooms at 10% still permit nine reservations; ten permit eleven; twenty permit twenty-two. Percentage is constrained to 0–10. Calendar changes cannot reduce capacity/policy below current occupied units: the same constraint rejects and rolls back the whole administration batch.

A `BEGIN IMMEDIATE` transaction locks this SQLite shard's writer, reads every night's current price, checks the exact expected total, conditionally increments each night's reserved units, creates the order and nightly price snapshots, and inserts its charge outbox job and HELD event. Any failed night, missing row, price mismatch or crash rolls the entire operation back. The conditional update repeats the capacity predicate in SQL; the CHECK constraint remains a final guard even against a bypass of application checks. Administrative price changes preserve previously saved order snapshots.

Pessimistic row locks in PostgreSQL could lock just the relevant dates in a stable order. Optimistic compare-and-swap could use the version field with conflict retry but suffers repeated retries on a hot hotel. This reference deliberately chooses SQLite's writer serialization plus conditional updates and constraints; its version field is observable metadata, not an implemented optimistic lock algorithm. No row-level lock throughput or comparison benchmark is claimed. Sharding separates different hotels' writers, while a single hot hotel remains serialized in one shard.

Quotes and availability reads are advisory. The user supplies `expected_total` in cents; a mismatch with the current transaction's sum × quantity returns 409 before any inventory changes. The accepted total and nightly prices are frozen at creation. Taxes/fees/exchange are omitted; currency is USD only. Stays use `[checkin,checkout)` date bounds, at most 30 nights and 10 rooms, within 730 UTC calendar days. UTC is an explicit simplification, not hotel-local timezone support.

## Idempotency and guest authorization

Order idempotency is scoped to `(hotel,user,request_key)` in a durable UNIQUE constraint. ID is `hotel_id + '.' + first_128_bits(SHA256(canonical_JSON([user,request_key])))`; hotel ID permits only ASCII letters/digits/underscore/hyphen so it can route directly. Body fingerprints prevent the same key from changing dates, room count, expected price or simulated payment method. Replay returns the current state of the same order, even after the stay date or price has changed, and never re-acquires inventory or charges again. The check occurs before fresh date/price validation for this purpose. Identities and receipts are not automatically expired.

Bearer tokens map to configured guest identities; the guest ID is never accepted from the booking body. Guests can read/cancel only their own orders, with missing/foreign IDs both returning 404. History is hotel-scoped with a bounded 100-item ID cursor, not a global cross-shard guest index or a snapshot across concurrent insertions. Admins manage all hotels in this reference and may inspect/cancel orders; per-hotel staff ACLs are a production extension. Admins create bookings using an actual configured guest credential rather than implicitly impersonating a guest.

## Order/payment state machine and compensation

Order states are PENDING, CONFIRMED, CANCELLED, EXPIRED and REJECTED. Separate payment states are NONE, CHARGING, PAID, REFUND_PENDING, REFUNDED and DECLINED. Creating an order holds all nights but does **not** confirm accommodation. Full simulated payment success before hold expiry makes PENDING → CONFIRMED/PAID. Explicit decline rejects and releases the hold. Payment timeouts/HTTP errors never mean decline: the outcome is unknown and the job remains durable for retry.

Cancellation is idempotent and atomically releases inventory exactly once using the order's `held` flag. Confirmed paid orders enqueue a full refund; pending orders retain any previously attempted charge job so an unknown outcome can be resolved. A first-ever charge attempt is skipped if the order has already been canceled or expired, avoiding an unnecessary initial charge. For previously attempted operations, retries may discover an earlier charge or complete an operation that had not reached the provider; if the order is no longer eligible, any success immediately enqueues a compensating refund. The asynchronous intermediate states are visible to the guest.

Workers sweep up to 100 expired PENDING orders per loop and release their held inventory before claiming another job. Expiry is effective when swept, not a hard real-time TTL guarantee: worker downtime can conservatively reduce availability until recovery. If a charge completes after expiry, settlement rechecks the wall clock inside the write transaction, releases inventory and schedules a refund rather than confirming. Order locking serializes cancel, expiry and settlement races. Confirmed orders do not expire as payment holds. This reference permits full cancellation/refund without check-in penalties; production cancellation policy is outside scope.

## Durable jobs, leases, and payment simulation

Charge job ID is `charge:<order_id>` and refund ID is `refund:<order_id>`. Jobs, order state changes and durable events commit with their corresponding inventory transaction. Claiming a job commits an attempt count and random lease token before contacting the payment service. A worker crash or lost callback leaves a lease that can be reclaimed; settlement checks the token plus the durable booking/action/amount to fence stale workers. Default lease is two seconds; idempotent provider operations tolerate overlapping requests if the lease expires during a slow HTTP call. HTTP work runs outside the inventory write transaction.

The simulator stores frozen operation fingerprints and results in its own SQLite transaction. Identical operations return their original receipt, with an attempt count; changed amounts under the same ID fail. Refund requires a paid matching charge and a durable refund operation ensures it happens once. The only methods are `test-ok` and `test-decline`; no card, bank or real payment SDK is present. A payment adapter interface makes the boundary explicit, but no real provider integration has been implemented or validated.

The test-only authenticated simulator control can return 503 **after** committing a payment result, or reject refunds temporarily. The first models missing confirmation and the second a refund service outage. Retries use bounded exponential delay (at most 25.6 seconds with the current exponent cap), persist errors, and continue until receipt settlement; financial uncertainty is never silently dead-lettered away. A failed shard callback is retried after the lease or outbox delay. Payment and shard restarts preserve operations/jobs on local disk. The durable events endpoint is a polling journal; it is not a delivery-guaranteed external notification bus. Job status exposes attempts and recent errors for operator attention.

## Caching and operational limits

Details and availability use a thread-safe bounded LRU (128 entries, one second TTL). Keys include the shard's durable revision. Changes made by the separate worker process increment that revision when inventory changes, so the HTTP process's cached availability is invalidated without shared memory. The cache still requires a cheap revision lookup and can race a concurrent commit; reads are advisory and writes always consult the transaction. Order/payment reads are uncached.

All services bind localhost by default; request JSON is limited to 1 MiB and HTTP client responses to 4 MiB. Distinct guest/admin/internal/payment credentials are mandatory; raw internal outbox/event APIs and simulator controls are not accessible through the public gateway. There is no TLS, rate-limit gateway, card handling, tenant isolation, encryption at rest or automated privacy/receipt retention. Configuration and `.runtime` are ignored, but deployment credentials still require operator protection.

SQLite shards, gateway configuration and simulated ledger have no replication, consensus or automatic failover. Tests exercise retained-disk process crashes, not host/disk loss. Workers can be restarted and leases recovered, but no orchestration leader election is supplied. The two-shard lab demonstrates physical data separation and routing, not automatic cluster growth or the book's 5000-hotel capacity. No search, real room assignment, dynamic pricing algorithm, real payments or production availability claim is made.
