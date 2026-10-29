# Tasks: Proximity Service

Source: chapter 17 of the root `System Design Interview.md`, section start line 5880.

Status: executable reference implemented; engineering review updated 2026-10-25.
Real local independent-process HTTP replication has been verified. Production
extensions do not count as deployment completion.

## Scope

Query businesses by latitude/longitude within a specified radius, view details,
and create/replace/delete owned businesses. Radius options: 0.5/1/2/5/20 km;
normal changes take effect at the next UTC-day publication.

## Implementation Tasks

- [x] T1. CRUD, nearby search, owner authentication, strict records/query/cursor validation and bilingual design.
- [x] T2. Base32 geohash and conservative spherical coverage, including antimeridian/poles and bounded precision reduction; stable great-circle atan2 distance filtering.
- [x] T3. Durable drafts/public/index/replay, atomic UTC-day generation, publication preparation outside the writer with head/revision fences, and owned DailyPublisher independent of traffic. First mutation after midnight freezes yesterday's drafts.
- [x] T4. Lineage/version-bound TTL/LRU caches, fixed-stripe cold-load coalescing, isolated mutable cache values and immutable canonical detail text; SQL candidate-body batches of 256.
- [x] T5. Single-SQLite logical hash partitions, independent-database HTTP replicas, immutable snapshot validation, region-confined reads/failover. Quadtree/S2 tradeoffs documented; physical sharding/cloud deployment remain extensions.
- [x] T6. Category/UTC opening filters, fixed requested radius and snapshot/query-bound pagination.
- [x] T7. Original 1,350-point/45-query boundary fixture and 400-point/30-query global fixture, plus independent 25,920-point coverage and 1,000-pair distance checks; cache, publication, rollback, cursor and migration regressions.
- [x] T8. 10,000-business dense/sparse measurements and real HTTP load with four clients/120 requests, retained as dated raw results. Bounded transport, strict framing/JSON, cancellation, admission and worker ownership have regression coverage.
- [x] T9. Self-cleaning four-process example, bilingual READMEs, chapter review and benchmark report. Root README/roadmap integration is maintained centrally; the original parallel-task integration note is historical.

## Verification Evidence

The current complete regression result and 2026-10-25 local/HTTP measurements are
recorded in [benchmarks/results.md](../benchmarks/results.md), with commands and
limits. [Chapter review](chapter17-review.md) maps requirements and new tests.

Historical baseline, executed **2026-10-13**, retained for comparison:

- `python -W error::ResourceWarning -m unittest discover -s tests -v`: 17 tests passed in 9.806s. This is the original suite, not the current test count.
- `python examples/cluster_demo.py`: primary/two replicas/gateway, three SQLite files, antimeridian distance 222.390m; reads retained after stopping a replica or primary.
- `python tools/benchmark.py --output benchmarks/local-results.json`: 10,000 businesses, publication 0.809s.
- `python tools/http_benchmark.py --output benchmarks/http-results.json`: four clients, 120 requests, 4.068s, 29.5 QPS, zero errors.

New reports are [review-local-2026-10-25.json](../benchmarks/review-local-2026-10-25.json)
and [review-http-2026-10-25.json](../benchmarks/review-http-2026-10-25.json).
Small local samples are not production soak or capacity evidence.

## Production Extensions Not Yet Landed

- [ ] P1. Physical sharding, real cloud regions/availability zones, database HA and write-primary election.
- [ ] P2. Incremental replication, persistent shared cache and candidate-hotspot mitigation. Batch detail loading is implemented in T4.
- [ ] P3. TLS termination, production identity/owner proof, sustained rate controls, backup/recovery, monitoring and alerting.
- [ ] P4. 200 million businesses, 5000 QPS and sustained fault/load acceptance; current local results do not prove these targets.

## Acceptance Thresholds

- Correctness, restart persistence, idempotency and failure paths have repeatable tests.
- Distributed capability is verified with independent processes/network; simulated faults are labeled separately.
- External cloud/vendor integrations require their own evidence; local interfaces are not deployment proof.
- The book's scale and availability are architecture targets, not claims proven by local tests.
- Submission requires a user request; the existing local-only convention for requirements/tasks documents is retained.
