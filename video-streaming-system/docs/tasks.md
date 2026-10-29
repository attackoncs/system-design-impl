# Tasks: Design YouTube

Source: root `System Design Interview.md`, Chapter 15, section starting line 4765.

Status: Reference implementation and PostgreSQL/S3 extensions completed, 40 tests fully passing; remaining production extensions are listed below.

## Scope

Upload and play videos up to 1 GB; multiple bitrate renditions across different clients; no recommendations, comments, or live streaming.

## Implementation Tasks

- [x] T1. Author requirements.md and design.md first, defining upload, transcoding, playback state machines and storage/media processing interfaces.
- [x] T2. Implement chunked/resumable upload, checksums, metadata submission and idempotent finalization; provide pre-signed upload interface, test expiration and privilege escalation.
- [x] T3. Implement persistent transcoding DAG, dependency scheduling, bounded process/codec resources, asynchronous workers, retries and persistent failure records; failure must not publish half-finished assets.
- [x] T4. Provide an actual media transcoding adapter and fixed test videos; acceptance of fake workers versus real transcoding is recorded separately.
- [x] T5. Generate multi-bitrate segments and playlists, support client bitrate selection, range reads and local object read boundaries; cloud CDN integration is listed as a separate extension.
- [x] T6. Implement temporary file reclamation, takedown, authorized reads and encrypted interface; document real key management and HTTPS termination requirements.
- [x] T7. Verify interrupted uploads, worker exit, duplicate tasks, source video checks, metadata rollback, missing object recovery and complete playback.
- [x] T8. Compare CDN costs for hot videos, origin costs for cold videos, and encoding/storage costs; measure local upload/HTTP read latency and short-video transcoding time.
- [x] T9. Provide runnable examples, English/Chinese READMEs, design trade-offs and actual tests/benchmark reports, update root-level status; unverified production scale remains explicit.

## Acceptance Thresholds

- Correctness, persistent restart, idempotency and failure paths have repeatable tests.
- Distributed capabilities must be validated with actual independent processes/network; simulated implementations are marked separately.
- External cloud services/providers use replaceable adapters; real-service validation must not be claimed without configured credentials.
- Original book scale and availability numbers are design targets, not directly proven by local tests.
- Submit only on user request; requirements.md/tasks.md under docs continue the convention of not being committed.

## Distributed Extensions and Follow-up Tasks

- [x] H1. PostgreSQL backend, cross-connection transaction mutex, primary discovery and reconnection on disconnect.
- [x] H2. S3 storage, independent node working directories, verified downloads and remote object reclamation.
- [x] H3. Automatic failover deployment and offline migration tooling; Compose topology verified, container run not verified.
- [x] H4. Real PostgreSQL/S3 network validation and primary failure drills; record environment limitations.
- [ ] Real CDN hot/cold distribution, cache warm-up and billing verification.
- [ ] GPU cluster scheduling, production capacity tests, HTTPS/KMS and commercial DRM.

Acceptance records: benchmarks/results.md; media examples examples/media_demo.py. Completion of the reference implementation does not indicate completion of these production extensions.

Distributed acceptance: benchmarks/ha-results.md; 40 passed; Patroni promotion 16.362 s,
original API recovery 16.582 s. Cross-physical-host network partition, object storage replication and production capacity not verified.
