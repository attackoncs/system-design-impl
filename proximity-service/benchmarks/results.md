# Validation report

## Core review — 2026-10-25 (Asia/Shanghai)

Windows 11 build 26100, Python 3.13.9. No third-party runtime or test packages.
From the project directory:

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -q
python examples/cluster_demo.py
python tools/benchmark.py --output benchmarks/review-local-2026-10-25.json
python tools/http_benchmark.py --output benchmarks/review-http-2026-10-25.json
```

The final suite passed **76 tests in 17.436s**, without skips or resource warnings:
17 original tests, 7 additional geography tests, 13 cache/model tests, 23 HTTP
reliability tests and 16 publication tests. Local networking and independent
process tests ran in the approved unrestricted local test process because of
Windows sandbox socket restrictions. Test temporary files were isolated in the
repository's `.test-tmp` directory and owned services were cleaned up.

New checks exercise cold-load coalescing, mutable record isolation, TTL start at
successful load completion, strict cursor/JSON/framing, total I/O deadlines,
region-confined failover, bounded admission and close draining. Publication
checks include draft-revision/CAS races, rollover during construction, reads of
the old complete generation, legacy five-table migration, immutable replica
versions, imported header ownership and background publication without traffic.
Geometry checks add 25,920 destination points (72 centers × five radii × 72
points), 1,000 random distance pairs and near-antipodal precision against an
independent unit-vector oracle. These still verify the spherical metric.

The four-process demo completed with pending total 0, published total 1,
antimeridian distance **222.39016046812768m**, and total 1 after a replica and
then the primary stopped. Three separate SQLite files were used. The original
independent-process suite also verifies restart, durable replay and retained
replica reads. Ruff E9/F, formatting, Python 3.10 syntax and local documentation
links were checked separately. See [chapter review](../docs/chapter17-review.md).

### Current in-process measurement

[Raw report](review-local-2026-10-25.json): the same seeded 10,000 synthetic
businesses and six cases as the historical benchmark, 30 repeated searches per
case. Publication **0.730s**, SQLite **6,033,408 bytes**. Initial queries may share
previous cases' caches; repeated dense queries are not fully warm when their
5,000 candidates exceed the default 2,048-entry detail cache.

| Area/radius | Candidates | Total | Cells | Repeated p50 ms | Repeated p95 ms | Sequential QPS |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Dense / 500m | 543 | 109 | 6 | 12.206 | 15.055 | 79.67 |
| Dense / 5km | 5000 | 5000 | 1 | 114.046 | 132.519 | 8.89 |
| Dense / 20km | 5000 | 5000 | 9 | 121.022 | 143.548 | 8.27 |
| Sparse / 500m | 0 | 0 | 4 | 5.860 | 6.342 | 170.80 |
| Sparse / 5km | 0 | 0 | 2 | 5.835 | 6.292 | 172.10 |
| Sparse / 20km | 0 | 0 | 12 | 6.157 | 6.704 | 160.38 |

An intermediate profile found dictionary deep copies dominated dense searches
when scan churn caused 5,000 detail-cache misses per request. Store now caches
immutable canonical JSON text and parses an independent object at the response
boundary. Candidate bodies load in SQL batches of 256. Cache's general copy,
coalescing, TTL and error contracts remain intact. This removes redundant record
copies; it does not remove dense-scan churn, full filtering/sorting or prove a
performance improvement against the historical run on a different host state.

### Current real HTTP measurement

[Raw report](review-http-2026-10-25.json): 4 clients, 120 localhost requests to
an independent primary process, six cases with 20 observations each. **3.961s,
30.30 QPS, zero errors**. The primary's daily publisher was enabled. This load
ran after the test suite and in-process measurement, not concurrently with them.

| Case | p50 ms | p95 ms |
| --- | ---: | ---: |
| Dense / 500m | 65.293 | 114.241 |
| Dense / 5km | 313.981 | 379.505 |
| Dense / 20km | 286.053 | 364.290 |
| Sparse / 500m | 36.248 | 59.727 |
| Sparse / 5km | 33.982 | 64.160 |
| Sparse / 20km | 46.158 | 61.053 |

The p95 convention remains sorted index floor(0.95×(n−1)). Sparse cases measure
empty-query overhead. HTTP includes parsing, thread contention, SQLite,
serialization and transport. This is a small local measurement, not a sustained
load, WAN or cloud-HA acceptance test. Inbound threads and outbound router work
are bounded, but DNS and database/CPU work may outlast socket cancellation.

## Historical observations

The observations below retain their original dates and implementation state.

Executed 2026-10-13 (Asia/Shanghai), Windows 11 build 26100, Python 3.13.9.

Reverified 2026-10-20: `python -m unittest discover -s tests -q`, 17 tests passed
in 9.740s. Current restricted Windows sandbox blocked localhost connections;
the successful run used the approved unrestricted local test process. Original
benchmark figures below were not rerun and retain their original measurement date.

## Tests and demo

`python -W error::ResourceWarning -m unittest discover -s tests -v`
completed **17 tests in 9.806s, all passing**, without unclosed connection warnings.

Coverage includes geohash known vector, invalid/nonfinite coordinates, deferred
create/move/delete, first write after UTC midnight, durable/concurrent replay,
owner isolation, replay expiry, publication rollback, cache TTL/LRU/version keys,
category/time filters, paging conflicts, persisted shard configuration, monotonic
replica import and duplicate-import rollback. Three concurrent readers during
eight publications see a single complete name generation per response.

The geometry fixture contains 1,350 destination points from nine centers, all five
radii, six bearings and five distances (0, radius−0.01m, radius, radius+0.01m,
1.4×radius). 45 indexed searches equal full haversine scans. Centers include
Shanghai, equator/prime meridian, ±180, near both poles and exact poles. Another
400-point seeded global fixture checks 30 queries. This verifies the service's
spherical metric, not ellipsoidal or road distances.

The network test starts independent primary, two replicas and gateway processes
with three separate databases. It checks malformed/unauthorized requests, owner
CRUD, authenticated snapshots, replicated updates, killed replica failover,
unavailable-region 503, unknown-region 400, primary shutdown, replica restart
with retained snapshot and primary restart with durable replay. No transport
mock or shared database is used. Replica restart was tested while primary was
offline; continued reattachment to a changed upstream address is not automatic.

A separate fault-injection unit test replaces transport and injects a transient
SQLite OperationalError, verifying that the polling loop survives and the next
snapshot succeeds. This simulated database fault is separate from the real HTTP
independent-process replication test above.

`python examples/cluster_demo.py` also completed: pending total 0, published total
1, antimeridian distance 222.39016047087586m, total 1 after one replica stopped,
total 1 after primary stopped. Four processes, three SQLite files; cleaned up.

Initial tests exposed unclosed initialization connections causing Windows file
cleanup failures; explicit closing fixed them. First-write midnight and persisted
shard regressions have dedicated tests.

## Dense/sparse measurement

`python tools/benchmark.py --output benchmarks/local-results.json`

10,000 seeded businesses: 5,000 within ±0.03 degrees of Shanghai, 5,000 global
random points. Publication **0.809s**, SQLite approximately **6.05MB**. One initial
query and 30 repeated queries per case, default page size 100. Computing total
still filters/sorts all matches. Initial queries may reuse earlier cases' caches.

| Area/radius | Candidates | Total | Cells | Warm p50 ms | Warm p95 ms | Sequential QPS |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Dense / 500m | 543 | 109 | 6 | 5.698 | 6.126 | 174.62 |
| Dense / 5km | 5000 | 5000 | 1 | 84.628 | 94.648 | 11.55 |
| Dense / 20km | 5000 | 5000 | 9 | 94.361 | 126.998 | 10.05 |
| Sparse / 500m | 0 | 0 | 4 | 4.404 | 5.302 | 221.06 |
| Sparse / 5km | 0 | 0 | 2 | 4.567 | 5.264 | 215.46 |
| Sparse / 20km | 0 | 0 | 12 | 4.662 | 5.244 | 210.92 |

Sparse queries have no matches and measure empty-query overhead. Details cache
capacity 2048 is smaller than the 5000-candidate dense scan, causing churn.
Caching does not automatically improve this case. These historical results
predate the 2026-10-25 batch hydration and immutable-text changes; adaptive dense
precision and connection pooling remain future performance work.

## Real HTTP load

`python tools/http_benchmark.py --output benchmarks/http-results.json`

Independent primary process, same synthetic layout, 4 concurrent clients,
120 localhost requests equally divided among six cases: **4.068s, 29.5 QPS,
0 errors**. Each case has 20 samples; p95 is sorted index floor(0.95×(n−1)).

| Case | p50 ms | p95 ms |
| --- | ---: | ---: |
| Dense / 500m | 74.289 | 97.128 |
| Dense / 5km | 369.978 | 462.737 |
| Dense / 20km | 321.399 | 409.063 |
| Sparse / 500m | 26.316 | 30.235 |
| Sparse / 5km | 18.167 | 27.087 |
| Sparse / 20km | 20.648 | 36.787 |

Tests and HTTP load ran concurrently; host contention affects timings. HTTP
includes parsing, thread contention, SQLite connections, serialization and
transport. This is a small local load experiment, not a sustained production soak.
Raw JSON is under this directory; rerun into `.runtime/` to retain observations.

## Unverified production extensions

No cloud regions, WAN, physical shards, Redis, MySQL/PostgreSQL, database leader
election, TLS proxy or production owner identity provider were used. Logical
shards and region routing are operational reference interfaces, not deployed
geographical clusters. Full snapshot client size is capped at 128MiB; publication
rebuilds all six index levels. The book's 200 million businesses / 5000 QPS remain
architecture goals. Backups, incremental replication, failover of writers and
real data residency controls are future work.
