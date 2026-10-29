# Tasks: Design Google Drive

Source: root `System Design Interview.md`, Chapter 16, starting at line 5369.

Status: Core reviewed on 2026-10-24, with 116 tests passing; independent
API/device processes, automatic scan synchronization and restart verified.
See [chapter review](chapter16-review.md) and [validation](../benchmarks/results.md).

## Scope

Upload/download of arbitrary files (up to 10 GB), multi-device synchronization,
revisions, sharing and notifications; document co-editing is out of scope.

## Implementation tasks

- [x] T1. Write requirements and design first, covering block storage, strongly
  consistent metadata, client synchronization and the notification service.
- [x] T2. Implement file chunking, checksums, compression, duplicate-block
  deduplication and a real AES-GCM encryption adapter; do not invent
  cryptographic algorithms yourself.
- [x] T3. Implement simple/resumable upload, download and revisions; visible metadata
  is published only after all blocks are reliably stored.
- [x] T4. Implement delta sync, device cursors, offline recovery, conflict
  copies and deletion tombstones, handling concurrent edits.
- [x] T5. Implement sharing permissions, revocation, quotas, revision browsing
  and persistent notifications; demonstrate synchronization using long
  polling/network adapters and a bounded, workspace-bound automatic scanner.
- [x] T6. Design metadata transactions, cache invalidation, block reference
  counting, a local cold-data archive/restore demo and safe garbage collection.
- [x] T7. Verify real multi-device processes, interrupted uploads, block
  corruption, metadata transaction failures, notification loss and duplication,
  and permission revocation.
- [x] T8. Report synchronization latency, deduplication savings and bandwidth,
  distinguishing the local reference implementation from cross-region storage
  durability.
- [x] T9. Provide runnable examples, English/Chinese READMEs, design trade-offs
  and actual test/stress reports; update the root status; unverified production
  scale stays explicit.

## Acceptance gates

- Correctness, persistence across restarts, idempotency and failure paths have
  repeatable tests.
- Distributed capabilities must be verified with real independent
  processes/networks; simulated implementations are labeled separately.
- External cloud services/vendors use replaceable adapters; when credentials
  are not configured, no claim of real-service verification may be made.
- The book's scale and availability numbers are design targets, not directly
  proven by local tests.
- Commit only after the user requests it; requirements.md/tasks.md under docs
  continue the convention of not being committed.

## Explicit follow-up extensions

- [ ] PostgreSQL primary/standby or sharding with replicated object storage,
  and a real cross-region durability exercise.
- [ ] Native file watchers, mobile clients, HTTPS/KMS and key rotation.
- [x] Bounded device block cache and server manifest cache, with live ACL checks.
- [ ] Production event retention/re-snapshot policy and sustained stress testing.

Acceptance: benchmarks/results.md; examples/network_demo.py. Currently not
committed or pushed.
