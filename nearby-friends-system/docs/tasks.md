# Tasks: Nearby Friends

Source: root System Design Interview.md chapter 18, lines 6726–7293.
Core review date: 2026-10-25. Final executed counts and measurements are in the
[validation report](../benchmarks/results.md); historical counts are not current.

## Completed core work

- [x] Define configurable 5mi radius, 600-second inactivity, mutual consent, current
  snapshots, distance/time, per-user ordering and separately consented history.
- [x] Real WebSocket location, friendship, sharing, history, ring and paging commands;
  initial snapshot plus enter/update/leave and periodic reconciliation.
- [x] Real Redis TTL mirror and per-user channels; consistent-hash broker routing,
  epoch CAS, reattachment and recovery after lost hints.
- [x] Split domain validation, immutable routing, schema, protocol and owned work pool.
  Add indexed expiry/reverse friendship, consistent cutoff reads and writer-time TTL.
- [x] Recheck SQL authorization after Redis operations. Repair with one MGET and
  batches of 128, a between-batch budget, finite pools and leased broker-client LRU.
- [x] Strict JSON/field validation, bounded frames/sends, application admission,
  bounded synchronous submissions, cancellation ownership and shutdown draining.
- [x] Write/fsync exclusive token files before SQL provisioning; failure cleans only
  the matching new file. Isolate real Redis with per-run passwords/run-ID cleanup.
- [x] Add transaction, backend, session/work and provisioning fault/concurrency tests;
  keep real independent-process tests and identify injected faults separately.
- [x] Synchronize bilingual READMEs/design/requirements/tasks and chapter mapping.

## Verification

Use the current [report](../benchmarks/results.md) for commands and final execution
results. The new raw fanout measurement is
[review-2026-10-25.json](../benchmarks/review-2026-10-25.json).

Historical baseline on **2026-10-20**: 12 tests passed (9 core, 3 real-network) in
16.160s; two WebSocket/two Redis processes, 21 connections, 200/200 observed
fanout deliveries, p95 140.323ms. These figures retain their measurement date.
Network tests explicitly skip without a configured real Redis executable.

## Remaining extensions

- [ ] Physical user/history/current-cache sharding, Redis primary/standby, SQL HA,
  consensus-backed discovery and cloud region/AZ deployment.
- [ ] A production Redis-authoritative fast path with revocation epoch/fence and
  recovery rules; the current mirror does not avoid two SQL report reads.
- [ ] Production TLS/identity/relationship service, ingress TCP/handshake quotas,
  load-balancer drain integration, backup recovery and observability.
- [ ] Sustained capacity/failure acceptance for 10M connections, 334K reports/s and
  the book's fanout estimate. Small localhost samples do not prove these targets.
- [ ] Optional nearby-stranger geohash channels or an Erlang/BEAM alternative if
  chosen explicitly; neither changes the current friend-sharing permissions.

Only submit when requested. Preserve the existing local-only convention for
requirements/tasks and unrelated local work.
