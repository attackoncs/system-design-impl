# Requirements: Nearby Friends (chapter 18)

Source: repository System Design Interview.md, chapter 18, lines 6726–7293.
Review date: 2026-10-25. See [chapter review](chapter18-review.md).

## Functional contract

- Return authorized nearby friends with location, spherical straight-line distance
  and last-updated Unix timestamp. Default radius 8046.72m (5mi), configurable up
  to 100km; no automatic expansion. Initial page size 20 and live offset paging.
- Use actual WebSockets for location commands, snapshots and enter/update/leave
  changes. A new connection sees current state; a new user supplies a location to
  seed presence. Suggest a 30-second client reporting interval, rather than an
  enforced server timer. Default inactivity timeout is 600 seconds.
- Require mutual friendship and opt-in sharing on both sides. Recheck authorized
  current SQL state after optional Redis work; a later revoke may race an in-flight
  frame. Sharing off clears current location and requires a new report on reenable.
- Require finite/range-checked location fields. Accept timestamps only within the
  TTL window and at most 30 seconds ahead, with strict per-user ordering across
  devices. Sample admissibility and receipt time after acquiring the SQL writer.
- Maintain real Redis TTL mirror keys and per-user Pub/Sub channels routed by
  consistent hashing. Hints may be lost. Reconnect, ring/friend changes and periodic
  reconciliation restore current state; do not promise event replay.
- History is a separate, self-only opt-in store, defaults to 24-hour retention and
  is deleted when disabled. Logically hide expired data and periodically sweep it;
  do not globally sweep on every location write.
- Support admin expected-epoch CAS ring changes. Keep ring inputs owned/immutable
  and validate Redis endpoints without permitting URL timeout/pool overrides.

## Resource and lifecycle contract

Bound application sessions, synchronous submitted work, Redis pools, broker-client
retention, cache-repair batches, input/output frames and sends. Defaults are 256
admitted sessions, 16 submitted synchronous calls, 16 connections per synchronous
Redis pool, 32 broker clients, 128 repair records per pipeline and a 1-second
between-batch repair budget. Cancellation must retain ownership/capacity until
accepted I/O exits. Close sessions/subscriptions/maintenance and drain work before
closing pools. Application admission is not a hard TCP quota, and budgets cannot
forcibly interrupt platform DNS or SQLite. Credentials are written to an exclusive
private file and fsynced before creating the SQL user. Do not log coordinates or
tokens in application access/error messages.

## Acceptance and boundaries

Use real separate WebSocket and Redis processes for transport evidence, covering
entry/exit, TTL, ordering, revocation, sharing off, reconnect, live paging, missed
hints and ring changes. Unit-injected faults must be labeled. The isolated lab
uses fresh Redis passwords and verifies run ID before shutdown; it stops owned
processes and retains diagnostic files in an ignored per-run directory.

The reference shares SQLite on one host for users/friendship, latest-current-row
recovery and consented history. Redis is a bounded mirror, and does not avoid SQL
reads. This differs from the book's Redis-authoritative fast path and does not
provide multi-host SQL HA, physical current-cache sharding or a production location
stream. Cassandra, user/history sharding, Redis primary/standby, consensus discovery,
cloud regions, TLS, production identity, backup recovery and 10M connections/334K
reports per second remain deployment or capacity extensions. Erlang is an
alternative; nearby strangers are extra credit, not authorization to reveal
friend coordinates to strangers. Current results are in
[validation](../benchmarks/results.md), not inferred from architecture estimates.

The existing local-only convention for requirements/tasks documents is retained.
