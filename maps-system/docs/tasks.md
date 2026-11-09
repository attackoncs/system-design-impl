# Tasks: Google Maps

Source: root System Design Interview.md chapter 19, starting at line 7294.
Latest review date: 2026-11-04; prior core review: 2026-10-25. Test totals are recorded in
[validation](../benchmarks/results.md), not inferred from implementation status.

## Completed reference work

- [x] Directed graph/mode/toll routing, fixture geocoding, batch ingestion and real
  GeoJSON/PNG with versioned public HTTP replication/gateway.
- [x] Write-lock-free canonical graph preparation, atomic install and persistent
  graph UUID identity; same-revision immutable catalogs, owned input and legacy migration.
- [x] Separate validation/schema/catalog/telemetry/cache responsibilities; indexed
  per-user hot retention and owned global maintenance rather than global ingest scans.
- [x] Writer-time timestamp/traffic TTL checks, recent traffic observations, stable
  sample tie handling and administrator-closure protection.
- [x] Isolated/coalesced caches and per-node adjacency copies; request-local node
  hydration, structured directions and precision-5 encoded polyline.
- [x] Classified local/arterial/highway routing views with parent/child/entrance
  references; fine-graph exact search retained rather than claiming hierarchy speedup.
- [x] Neighbor tile URL service, bounded cold rendering, polar/dateline fixes and
  independent geometry checks.
- [x] Persistent immutable/checksummed precomputed tile archive with atomic bounded
  batches and active-version reads; local object-store reference only.
- [x] Private expiring navigation, affected-area refresh and periodic full refresh,
  CAS updates and finite SSE current-state reconnect pages.
- [x] Bounded strict HTTP/direct transport, total gateway budgets, same-region
  failover, no credential redirects and accepted-I/O shutdown draining.
- [x] Credential-file ownership, bind-before-start helpers and retrying maintenance;
  synchronize bilingual READMEs/design/requirements/tasks and chapter mapping.
- [x] Re-read chapter 19, compare pinned Valhalla/OSRM source and trace the current
  routing, tracking, telemetry, archive and HTTP boundaries with CodeGraph.
- [x] Repeat 113 tests with `-W error`, both real-process examples and the bounded
  mixed HTTP measurement; record scope and raw results without a speedup claim.
- [x] Document source-review conclusions and add the navigation CAS sequence to
  the learning guide; synchronize bilingual documentation and project indexes.

## Evidence

Current [report](../benchmarks/results.md) includes actual counts, real-process
versus injected faults, commands and [dated raw data](../benchmarks/review-2026-11-04.json).
The [source comparison](open-source-review_cn.md) found no confirmed source defect
in this review; the production extensions below remain open.
Historical baseline on 2026-10-20: 14 tests in 8.650s; 225 node-pair Floyd–Warshall
comparisons, 90 reads during eight swaps, real HTTP primary/replica/gateway.
A 900-node/3480-edge synthetic graph measured 60 requests/four clients, zero
errors and 72.17 mixed QPS. These retain their original date and implementation.

## Remaining extensions

- [ ] Licensed real road ingestion, turn/access restrictions, HMM matching,
  scheduled transit, learned traffic forecasts and a proved hierarchical search accelerator.
- [ ] Kafka/Cassandra or other physical stream/history/graph partitions,
  incremental replication, database write failover and archive retention/GC policy.
- [ ] Production identity/TLS, deployment admission/telemetry, actual regions/CDN,
  object replication, backup recovery and sustained book-scale acceptance.
- [x] Byte budgets for in-process caches (2026-11-09): rendered tiles 32 MiB and
  routing-tile adjacency 16 MiB by default (`--tile-cache-bytes`,
  `--adjacency-cache-bytes`), sized once at insert, LRU-evicted, entry limits kept.
- [ ] Process RSS limits, MVT/gzip, complex styles/fonts and full mobile
  viewport/navigation client; multi-stop optimization if explicitly selected.

Only submit after a user request; preserve unrelated work and the existing local
requirements/tasks convention. See [chapter review](chapter19-review.md).
