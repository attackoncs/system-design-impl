# Tasks: S3-like Object Storage

Source: Chapter 25 of `System Design Interview.md` in the project root, starting at line 11925.

Status: core engineering review complete (2026-10-26);137 tests passed in40.784s. Production requirements remain explicitly incomplete.

## Scope

Bucket management, object upload and download, versioning and listing; mixed small and large objects. Book targets: 100 PB scale, six-nine durability, and four-nine availability.

## Implementation Tasks

- [x] T1. Finalize requirements/design; clarify versions/markers, atomic publish, and metadata/data separation.
- [x] T2. Implement private bucket/object CRUD, whole-object validation, and version-specific reads; no publish when write acknowledgements are insufficient.
- [x] T3. Implement up to 64 MiB / 64-part multipart upload with resume, end-to-end validation, persistent completion intent, and expiry cleanup; no claim of GB-scale streaming capability.
- [x] T4. Implement small-chunk pack file, offset index, tail recovery, index rebuild, and generational compaction.
- [x] T5. Implement dual metadata shards, sorted merge, and signed pagination token over frozen results.
- [x] T6. Implement three independent nodes, default 2-ack policy, and replica repair; erasure coding is discussed in documentation but not claimed as real failure-domain/durability SLA.
- [x] T7. Implement version retention, markers, reference protection, scrubbing, unreferenced-chunk reclamation, and compaction.
- [x] T8. Validate node exit, logical directory unavailability, byte corruption, publish interruption, upload resume, and pagination changes; hardware/power-loss faults are not validated.
- [x] T9. Deliver six-process example, bilingual README, design and measured-performance reports, and root-directory updates.

## Acceptance Thresholds

- Correctness, persistent restart, idempotency, and fault paths are covered by repeatable tests.
- Distributed capabilities must be validated with real independent processes and networking; simulated implementations are labeled separately.
- External cloud services/providers must use replaceable adapters; do not claim real-service validation when credentials are not configured.
- The book's scale and availability numbers are design targets and are not directly proven by local tests.
- Commit only when the user requests; `requirements.md` and `tasks.md` under docs follow the convention of not being committed.

## Acceptance Results and Boundaries

- 6 core + 4 network tests passed (29.924 seconds); after diagnostic fixes, the related replica-repair cases passed again (6.295 seconds).
- With 40 small objects (~4 KiB each), each node used only one aggregated pack file; PUT p95 79.572 ms, GET p95 70.359 ms. Measured numbers do not represent production scale.
- API/metadata publish interruption can resume `completing`; logical scenarios with three corrupted/lost replicas can be repaired. See `../benchmarks/results.md` for details.
- Single directory and metadata coordinator have no replication/leader election yet; no erasure coding, cross-datacenter failure domains, GB-scale streaming interface, or production SLA.
- This chapter is completed locally and not yet committed; Chapter 24 was committed as `fb2a429` and not yet pushed.

## 2026-10-26 review

- [x] Owner buckets, delimiter/common-prefix listing and query-bound stable pagination.
- [x] Freeze metadata, transactional upload state and version/receipt contracts.
- [x] Shared bounded coordinator/data reads, owned pool and deadline/cancel/drain.
- [x] Fence failed pack/index/CURRENT recovery; durable unknown publication intents.
- [x] Generation-checked node sweep prevents delayed stale collection deleting new PUTs.
- [x] 137 tests passed40.784s(-Werror); real six-process demos and four-client measurements.
- [ ] Production placement/consensus, metadata HA, pack rotation/per-core streams,
      streaming GB objects, erasure coding, cloud security and 100PB/SLA validation.
