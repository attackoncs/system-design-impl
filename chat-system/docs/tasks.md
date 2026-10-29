# Implementation Plan: Chat System

Complete requirements and design first, then implement and validate in this order.
Checkboxes reflect completed verification, not merely the existence of a file.

- [x] 1. Read the local Chapter 13 and define the scope.
  - [x] Write `requirements.md`, `design.md`, and this plan.
  - [x] Distinguish the book's distributed scale from the local reference implementation.
- [x] 2. Package and foundational models. (R1–R7)
  - Create the independent Python package, configuration validation, public API, and errors.
- [x] 3. Persistent message store. (R2–R4)
  - Implement direct/group membership, atomic inbox fanout, increasing IDs, and idempotency.
  - Verify file-backed restart, history isolation, paging, and retry conflict handling.
- [x] 4. Authenticated multi-device messaging. (R1, R3, R4)
  - Implement sessions, device replacement, live fanout, bounded queues, sync recovery.
  - Verify unauthorized access, revoked tokens, concurrent sends, and slow clients.
- [x] 5. Heartbeat presence. (R5)
  - Implement aggregated online status, timeout expiry, explicit logout, subscriptions.
  - Verify fake-clock transitions and multiple-device behavior.
- [x] 6. Offline push and discovery. (R6, R7)
  - Implement recording/provider interface and isolated failure recording.
  - Implement region/capacity selection; verify full and removed servers.
- [x] 7. Optional real WebSocket adapter. (R7)
  - Add JSON protocol, authentication timeout, errors, background event pump and cleanup.
  - Verify actual loopback connections, events, sync, malformed data, logout, and grace period.
- [x] 8. Examples and documentation. (R1–R7)
  - Add executable core and WebSocket server examples.
  - Write English/Chinese READMEs with recovery and deployment limitations.
  - Update root English/Chinese READMEs after implementation passes validation.
- [x] 9. Final validation.
  - Run the complete module tests and examples; check source compilation and diff formatting.
  - Report exact results and any limitations; leave changes uncommitted for review.

## Verification results

- `python -m pytest -q`: **44 passed**, including five actual loopback WebSocket tests.
- `python examples/basic_chat.py`: passed; offline push invocation, independent device
  sync, a live reply, cursor continuation, and group creation exercised.
- `python -m compileall -q src examples tests`: passed.
- Python 3.9 syntax parsing: passed for all source/example modules. Runtime tests
  executed on Python 3.13; Python 3.9 runtime compatibility is not separately verified.
- Editable installation with development dependencies: passed.
- Tracked diff format check: passed. No commit or push performed.

| Requirements | Verification focus |
|--------------|--------------------|
| R1 | Invalid/revoked tokens, same-device replacement, authenticated WebSocket login |
| R2 | Direct pair reuse, owner authorization, 100-member limit, pre-join history isolation |
| R3 | Retry conflict, concurrent deduplication, transaction rollback, text limits, slow-client recovery |
| R4 | File-backed restart, independent device cursors, pagination and access isolation |
| R5 | Fake-clock device aggregation, presence subscriptions, transport grace and background expiry |
| R6 | Offline/online recipient routing, deduplicated push invocation, provider failure isolation |
| R7 | Region/capacity selection, real WebSocket delivery, group/history protocol, errors and logout |

## Distributed extension tasks

- [x] D1. Extend requirements and design before the distributed implementation.
- [x] D2. Implement asynchronous Redis storage and atomic message/broker/outbox commit. (R8)
- [x] D3. Implement shared credentials, fenced device sessions, node leases, capacity, presence. (R9)
- [x] D4. Implement per-node durable event consumption and cross-node live fanout. (R8)
- [x] D5. Implement leased discovery HTTP API and a reconnecting cursor client. (R9)
- [x] D6. Implement reclaimable notification workers, retries, dead letters, HTTP adapter. (R10)
- [x] D7. Add runnable deployment configuration and update both language READMEs. (R11)
- [x] D8. Verify real Redis and independent-process failure scenarios; rerun local tests. (R11)

## Distributed verification results

- Complete suite with real Redis configured: **55 passed**, including the original
  44 tests, ten Redis integration tests, and one additional Unicode byte-bound test.
- Redis 8.0.5 ran as a temporary loopback-only server in the existing Ubuntu WSL
  environment. Package binaries were extracted under the ignored project runtime
  directory; no system service was installed.
- Independent Python node processes exchanged direct/group messages over actual
  WebSocket connections. Killing one process exercised node lease expiry, shared
  presence, discovery replacement, client reconnection, and persisted cursor recovery.
- Shared Redis tests verified concurrent retries, atomic stale-session write rejection,
  capacity checks, node-instance fencing, token revocation, and pre-join history privacy.
- Notification tests exercised actual HTTP POST delivery to a local endpoint, offline
  filtering, retry state recovery by a replacement worker, and dead-letter exhaustion.
- A real Redis shutdown/restart with AOF restored messages, inboxes, credentials,
  broker entries, notification jobs, and persistent deduplication keys. Retrying the
  original message after restart did not create another push job.
- Redis network failure did not acknowledge an in-memory fallback write. Large
  Unicode sync responses were bounded by bytes and continued through the returned cursor.
- Source compilation and Python 3.9 syntax parsing passed. Runtime tests used Python
  3.13, not Python 3.9.
- Compose YAML parsed and its five services were checked. Docker build/start was not
  executed because Docker is unavailable on this host; direct independent-process
  execution is the verified deployment path.
- No high-scale load test, Redis automatic primary failover, or cross-region quorum
  deployment was performed. Those are explicitly outside the supplied deployment.
- Changes remain uncommitted and unpushed.

## High availability and capacity tasks

- [x] H1. Define Sentinel failover, write-confirmation, and capacity acceptance criteria.
- [x] H2. Add Sentinel-aware async backend and same-connection replication confirmation.
- [x] H3. Replace admission/discovery scans with fenced per-node capacity indexes.
- [x] H4. Supply persistent primary/replica/Sentinel deployment and environment configuration.
- [x] H5. Add actual primary-failure promotion and continued-message tests.
- [x] H6. Add a configurable WebSocket capacity harness and measured local report.
- [x] H7. Run regressions and document exact results and remaining scale limits.

## HA extension final verification

- Final suite: 58 passed with direct Redis and fresh real Sentinel lab configured.
- 1,000 real WebSocket connections / 10,000 sends: 538.12 acknowledgements/s; live p99 94.08 ms; no missing/duplicate deliveries.
- Own chat-node termination: 500 connections re-established in 0.906 s; 10,000 sends verified without missing durable messages.
- Real primary process kill: automatic promotion and continued chat; uncertain idempotent retry rejected while replica paused.
- Results and reproducible commands: benchmarks/results.md and three JSON reports.
- Compose parsed; Docker execution unavailable. No sharding or 50-million-user capacity claim.
- HA extension is uncommitted and unpushed; the preceding distributed implementation is in commit 52e1d4f.
