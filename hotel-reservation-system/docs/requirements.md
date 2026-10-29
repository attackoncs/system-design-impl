# Requirements: Hotel Reservation System

Source: [System Design Interview.md](<../../System Design Interview.md>), chapter
23, lines 10507–11226. Review date 2026-10-26. Final evidence and unimplemented
book extensions: [validation](../benchmarks/results.md),
[chapter review](chapter23-review.md).

1. Manage hotel and room-type details with authorized create/update/soft-delete;
   reserve room types, not specific physical room numbers. Hotel search is out of scope.
2. Return per-type availability and a consistent hotel-wide date/quantity query,
   with exact daily prices, complete calendars and bounded source-type counts.
3. Offer optional persistent rolling calendar defaults up to 730 UTC days; fill
   missing dates only, preserve existing prices/occupied inventory, reject stale
   generation/version/cursor publication, and keep old rows on policy disable/shrink.
4. Atomically reserve every night in [checkin,checkout), 1..30 nights/1..10 rooms,
   within 730 future UTC days. Missing/full nights must roll the entire operation back.
5. Occupancy includes pending holds and confirmed bookings. It stays within
   capacity + floor(capacity*overbook/100), with overbook 0..10; administration
   cannot reduce the limit below occupied units.
6. Use USD integer cents and immutable nightly order snapshots; recheck the user's
   accepted total in the booking transaction and reject changed quotes.
7. Deduplicate by hotel/guest/request key and frozen payload. Replay returns the
   existing order's current state, never reviving cancelled/expired holds.
   Guest identities come from credentials; foreign/missing orders both return 404.
8. Colocate inventory, order and durable charge intent within one shard transaction.
   Payment success before hold expiry confirms; an explicit decline releases;
   unknown outcomes stay retryable and late success requires compensation.
9. Validate simulated jobs/receipts, amount, operation identity and lease fencing.
   Cancellation/refund must be idempotent, with bounded expiry cleanup and no
   silent discard of financially uncertain jobs. No real funds are used.
10. Use snapshot/revision advisory caching, single-flight cold loads, returned-value
    ownership and indexed bounded maintenance. Database constraints remain the write guard.
11. Bound HTTP admission, framing, JSON, request/response bytes and I/O time; only
    explicit public routes may be proxied, identity is preserved and writes are not
    blindly retried. Own, observe and drain handler/background work.
12. Use independent HTTP processes and actual local disks for integration evidence;
    controlled fault/provider tests remain labeled. Do not equate local results
    with production scale, bank correctness or replicated durability.

Production extensions remain replication/HA, online resharding, distributed cache/
CDC, global guest indexing, real payment integration, hotel-local policies/taxes/
currencies, tenant staff ACLs, TLS/quotas, privacy retention, disaster recovery and
sustained verification of the book's 5000 hotels / one million rooms.
