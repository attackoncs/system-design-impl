# Maps System verification

## Core review — 2026-10-25 (Asia/Shanghai)

Python 3.13.9 / Windows 11 build 26100, standard library only; SQLite RTree is
available. Real localhost networking used an approved unrestricted local process
because Windows sandbox socket restrictions block these tests. Temporary paths
were isolated in the workspace; owned service processes were stopped. Diagnostic
SQLite/log files remain in ignored `.runtime/lab-<uuid>/` directories.

### Final regression

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -q
```

**113 tests passed in 12.918s, no skips and no ResourceWarning**. The original
14-test baseline passed in 8.438s before changes. The final suite contains:

| Area | Tests |
| --- | ---: |
| Original maps / independent-process network | 14 |
| Store, telemetry, version identity and cache | 21 |
| Navigation / instructions / encoded geometry | 8 |
| Geography, rendering, layers and viewport URLs | 11 |
| HTTP, direct transport, gateway, replication and SSE framing | 23 |
| Persistent tile artifacts | 13 |
| Active navigation sessions and adaptive events | 14 |
| CLI, credential failures and maintenance ownership | 9 |

The independent Floyd–Warshall oracle still checks 225 node pairs. Original
concurrent readers remain consistent across eight graph swaps. New checks cover
same-version catalog conflicts, cross-restart UUID reuse, graph preparation
outside the writer, lock-delayed TTL validation, input ownership, rollback,
offline reports, latest received samples and preservation of live road closures.

Additional geometry uses 500 seeded point pairs against an independent unit-vector
reference, polar roundoff, dateline matching and clipping. Polyline is checked
against a known vector and decoded independently. Layers actually distinguish
local/arterial/highway roads; the route engine still uses the accurate fine graph,
without claiming hierarchical search acceleration or complete K-shortest paths.

Artifact tests generate real PNG/GeoJSON, reopen the archive and prevent spatial
queries/rasterization to prove persistent hits. They check corruption/size before
BLOB reads, immutable batch rollback, concurrent publication, bounded iterator
capture and graph switches. Private navigation tests use real SQLite for owner,
TTL, restart, CAS, closure/expiry/unreachable/graph changes and periodic discovery
of newly faster paths. Real HTTP verifies finite SSE delivery and authorization.
Injected slow work and publication faults are labeled controlled faults; separate
process/network tests remain actual HTTP with independent catalog files.

Ruff E9/F, formatting, Python 3.10 syntax and local Markdown links were checked
separately. See [chapter review](../docs/chapter19-review.md) for graph analysis
and requirements, including remaining production extensions.

### Executable examples

```powershell
python examples/network_demo.py
python examples/adaptive_demo.py --output benchmarks/features-2026-10-25.json
```

The original three-process/two-catalog demo still completed: route ab→bc,
44.48s ETA; a closure rerouted to ad→de→ec. Three routing tiles were referenced,
world PNG was 794 bytes, vector output had nine clipped features, and the gateway
retained public revision 2 after primary shutdown.

The [new feature report](features-2026-10-25.json) records actual CLI precomputation
followed by an independent primary process and authenticated HTTP/SSE:

- Two explicit XYZ tiles produced four PNG/GeoJSON objects, **4517 bytes** total.
- Viewport lookup returned nine version-pinned neighboring URLs.
- Closing bc caused a background replan from ab→bc to ad→de→ec, delivered as
  SSE revision 2 without another explicit route request.
- The new route had three structured steps and an encoded polyline; PNG response
  was 794 bytes with image/png type; deleting the private session succeeded.

This is synthetic fixture navigation, not directions from a live map provider.
The local archive is a persistent object-store reference, not a deployed CDN.

### Current mixed HTTP measurement

```powershell
python tools/benchmark.py --output benchmarks/review-2026-10-25.json
```

[Final raw report](review-2026-10-25.json): 900 synthetic nodes / 3480 directed
edges, import **119.018ms**, SQLite with sidecars **1,024,000 bytes**. Four clients
sent 60 requests to an independent primary, 20 each of route/GeoJSON/PNG:
**1.169s, 51.31 mixed QPS, zero errors**. The graph spans 42 fine routing tiles;
a short route referenced two. The final benchmark ran after the final test suite,
not concurrently with it.

| Operation | Count | p50 ms | p95 ms | Response bytes |
| --- | ---: | ---: | ---: | ---: |
| Corner route | 20 | 151.124 | 337.529 | 18054 |
| GeoJSON tile | 20 | 23.882 | 29.375 | 433554 |
| PNG tile | 20 | 14.444 | 173.946 | 1253 |

A diagnostic profile found repeated nested adjacency copies dominated warm route
CPU. Per-node immutable JSON cache values preserve isolation and coalescing while
parsing independent edge records for callers. The initial in-turn HTTP sample
was 36.20 mixed QPS; the final result above followed that optimization. These are
small observations, not controlled capacity experiments. In particular the old
route response below was 2553 bytes, while the new route includes instructions
and encoded geometry at 18054 bytes. Host load, cold tiles and scheduler variance
also differ, so neither comparison establishes a stable speedup against history.

P95 is sorted index floor(0.95×(n−1)) with only 20 observations per case. GeoJSON
remains verbose and uncompressed; these directed grid geometries do not establish
vector bandwidth savings. There was no WAN, cloud load, sustained soak or book-
scale throughput validation.

### Operational boundaries

HTTP defaults to 32 handlers / a 10s socket budget. Event waits use at most three
quarters of capacity, return the latest state or a heartbeat, then close for
Last-Event-ID reconnection. They are finite SSE pages, not a durable event log.
Active navigation defaults to 128 sessions, 900s lifetime and a 1s refresh tick;
route work and SQL may extend the tick. Region prefiltering is conservative and
periodic full recomputation catches improvements outside the current route area.

Precomputation is at most 256 tiles /64MiB per batch, archive publication at most
512 objects /64MiB, and each object at most 16MiB. Catalog and archive must use
separate files. Generation is complete and immutable, but two databases cannot
atomically bind archive publication to current catalog head: a switch after the
last check can leave a complete old generation. Origin reads still check active
graph identity; old retained artifacts require a separate GC policy. New rendering
styles require a new graph URL generation rather than overwriting existing keys.

Caches are entry-bounded, not byte/RSS budgets. Dense fine adjacency tiles and
catalog construction cost memory proportional to input. Cold rendering and
layer exports have explicit work caps; failures may return 413. DNS, SQL and CPU
work cannot be forcibly interrupted by socket deadlines; shutdown drains accepted
work. File-first credentials handle ordinary failures, not filesystem/SQL crash
atomicity. Licensed roads, production turn/transit rules, learned ETA, proven
hierarchical acceleration, Kafka/Cassandra, physical shards, writer HA, TLS,
real object-store replication/CDN and original-book scale remain unverified.

## Historical verification — 2026-10-20

The observations below retain their original implementation and date.

2026-10-20, Asia/Shanghai. Python 3.13.9 / Windows 11 build 26100. Standard
library only; SQLite RTree is available in this interpreter. Network tests used
approved unrestricted localhost processes because the current Windows sandbox
blocks loopback networking. Core tests also ran with workspace-local TEMP/TMP.

## Tests

`python -m unittest discover -s tests -v`: **14 tests passed in 8.650s**.

- Directed roads, one-way unreachable cases, toll avoidance, driving/walking mode
  constraints, alternative candidates and distinct search-budget errors.
- Baseline ETA, traffic slowdown/closure, expiry and explicit fresh-request replans.
- A seeded 15-node random directed graph compares all **225 origin/destination
  pairs** with an independent Floyd–Warshall oracle, including unreachable pairs.
- Eight concurrent graph replacements with 90 reader routes; graph IDs, chosen
  edge lists and costs all match one complete expected snapshot.
- Import validation and injected transaction failure rollback, restart persistence.
- Projection clamping/wrap, antimeridian split and seam clipping. PNG signature,
  IHDR dimensions, every chunk CRC and zlib-decoded scanline size are verified.
- Versioned render caches, routing-tile references, removed graph URL 410.
- Atomic location replay/conflicts, late sequences, self-only history and 24h pruning.
- Heading-aware directed matching, ambiguous-heading exclusion, two-user mean-speed
  aggregation. Cache/license/hash/lineage and rollback rules on replica installation.
- Real primary, replica and gateway processes with **two separate SQLite files**:
  authenticated traffic replication, ETag 304 through gateway, region restrictions,
  primary termination with continued route/tile reads, replica restart while
  primary remains offline, malformed requests, authorization and HTTP graph swaps.

Transport is actual HTTP; no mocked network, database cluster or CDN is claimed.
The import-failure hook is synthetic fault injection. SQLite shared-memory/WAL
behavior is not evidence of a production distributed database.

## Demo

`python examples/network_demo.py` completed with three processes and two databases:
initial route ab→bc with **44.48s** ETA; after closing bc the replica selected
ad→de→ec. Original route loaded **3** routing tiles. World PNG output was **794
bytes**, GeoJSON contained **9** clipped features. Gateway still returned catalog
revision **2** after primary shutdown. Tile response included
`public, max-age=31536000, immutable`. No real cloud cache/CDN was contacted.

## Measured synthetic workload

`python tools/benchmark.py --output benchmarks/http-results.json`

30×30 generated grid: **900 nodes, 3480 directed edges**, CC0 synthetic data.
Import **167.880ms**, SQLite including sidecars **905,216 bytes**. The graph spans
**42** z16 routing tiles; a short local route loads **2**, demonstrating lazy
partition loading. Long corner-to-corner Dijkstra can still reach most of the
small graph; hierarchical acceleration is not implemented.

4 client threads, independent HTTP primary, 60 requests equally mixed among
route, repeated GeoJSON tile and repeated PNG tile. **0.831s, 72.17 mixed QPS,
zero errors**. First tile render is cold and subsequent renders reuse LRU entries.

| Operation | Count | p50 ms | p95 ms | Response bytes |
| --- | ---: | ---: | ---: | ---: |
| Corner route | 20 | 96.443 | 150.319 | 2553 |
| GeoJSON tile | 20 | 17.513 | 65.708 | 433554 |
| PNG tile | 20 | 15.834 | 143.395 | 1253 |

p95 is sorted index floor(0.95×(n−1)), only 20 samples per operation. Bodies are
uncompressed HTTP payloads. This verbose GeoJSON represents overlapping directed
grid roads and is much larger than a simple compressed grid PNG; it does not
demonstrate vector bandwidth savings. Production MVT/protobuf, geometry merging,
gzip and detailed styling require separate measurements. Raw report is
[http-results.json](http-results.json); reruns should use `.runtime/` to retain
the recorded evidence. No QPS extrapolation or sustained capacity claim is made.

## Remaining production extensions

No licensed real-world road import, turn restrictions, accurate HMM matching,
transit schedules, learned ETA, contraction hierarchy, Kafka/Cassandra, incremental
replication, database writer HA, production identity, TLS, real CDN, geographic
deployment or book-scale traffic was validated. Full catalog transfers are capped
at 64MiB; HTTP writes at 2MiB; reference importer at 100K nodes/500K edges. Cache
bounds are entry counts, not byte limits. Retention is logical at query time and
physically pruned during active operation, not a compliance certification.
