# Maps System verification

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
