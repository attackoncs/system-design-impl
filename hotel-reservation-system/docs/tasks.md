# Tasks: Hotel Reservation System

Source: chapter 23 of [System Design Interview.md](<../../System Design Interview.md>),
lines 10507–11226. Core review 2026-10-26. Final executed counts and measurements
are in [validation](../benchmarks/results.md); [chapter mapping](chapter23-review.md)
records production gaps. The 2026-10-20 result remains historical.

## Completed reference work

- [x] Define room-type/day inventory, explicit floor-based overbooking, prices,
  reservation/payment states, guest authorization and transaction ownership.
- [x] Catalog/room-type CRUD, soft-delete, daily administration and immutable prices.
- [x] Multi-night atomic reservations, accepted-total check, durable idempotency,
  cancellation, expiry, decline and unknown-payment refund compensation.
- [x] Two independent hotel shards, a simulated payment ledger, frozen operation
  contracts, lease fencing, owned worker cancellation/drain and durable events.
- [x] Split schema/catalog/booking/saga/cache/transport/routes/lifecycle responsibilities;
  capture request ownership before writer waits and validate clocks after acquisition.
- [x] Snapshot/revision cache loaders with fixed-stripe cold-load coalescing;
  indexed/bounded history, jobs and expiry maintenance.
- [x] Hotel-wide active room-type availability over complete date/quantity ranges.
- [x] Optional rolling policies: bounded background fill, persistent non-reused
  generation/version/cursor, idempotent config, missing-day-only insertion and disable.
- [x] Explicit gateway route/method/query contract, body-before-auth prevention,
  strict JSON/framing, bounded direct RPC, admission and shutdown ownership.
- [x] Add behavior regression for confirmed persistence/concurrency/payment/network
  faults; retain independent six-process integration and actual process-exit cases.
- [x] Update English/Chinese README/design and local requirements/tasks; link final
  current evidence rather than presenting historical test counts as current.

## Remaining production work

- [ ] Replicated shards/ledger, consensus metadata, online resharding and failover.
- [ ] Real distributed cache/CDC, global guest index and workload throttling.
- [ ] Real payment provider, hotel-local timezones/cancellation policy, taxes/currencies.
- [ ] TLS/tenant ACL deployment, privacy/receipt GC, backups and disaster recovery.
- [ ] Sustained million-room/peak workload and filesystem power-loss verification.

Submit or push only when requested. Existing convention keeps requirements/tasks
local; this review does not imply a commit. SQLite serialization plus conditional
updates/CHECK is implemented; row locks/optimistic retry/2PC are alternatives,
not additional claimed implementations.
