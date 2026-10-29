# Requirements: Google Maps, chapter 19

Source: repository System Design Interview.md, chapter 19 (7294–8019). Core scope
is batch location updates, navigation/ETA and map rendering. Use independent
standard-library Python/SQLite components and a small licensed synthetic fixture;
photos/business data and multi-stop optimization are outside the initial scope.

- Import a validated directed graph atomically, with WGS84 nodes, positive road
  length/mode speeds, toll flag and optional local/arterial/highway classification.
  Preserve one-way, mode and avoidance constraints; distinguish unreachable from
  search-budget exhaustion and keep each route in one graph/traffic snapshot.
- Return geometry, encoded polyline and reproducible geometric directions/ETA.
  Candidate generation is bounded single-edge avoidance, not full K-shortest paths
  or learned prediction. Fixture geocoding and nearest-node snapping are bounded.
- Load fine adjacency lazily, expose classified z16/z12/z8 routing views with
  cross-tile/cross-level entrances, and keep exact search on the fine graph.
- Batch authenticated locations with durable per-user replay, atomic conflict,
  nonregressing sequence/time, receipt-time retention and direction-aware matching.
  Derive live samples only from recent valid driving observations; aggregate
  distinct users and preserve unexpired administrator closures.
- Provide private, expiring active navigation sessions. Refresh affected routes
  from traffic/graph changes and periodically check for new faster alternatives;
  preserve concurrent moves/deletes with CAS. Finite SSE current-state delivery
  supports owner checks, reconnect cursors and coalescing, not durable replay.
- Render actual GeoJSON/PNG at z0..21, with polar clamping, antimeridian clipping,
  unique neighboring tile URLs, versioned URLs/ETag and bounded cold work.
  Support a separate persistent immutable/checksummed precomputed archive, bounded
  atomic preparation, and active-version origin checks.
- Use bounded isolated/coalesced caches, handler/event capacity, direct I/O,
  strict JSON/framing, private authentication before body reads, same-region
  public failover and owned/drained background tasks.
- Replicate public catalogs over real HTTP into separate databases; reject changed
  same-version content, UUID reuse, rollback/foreign lineage and partial installs.
  Exclude credentials, locations, replay and navigation state. Do not access-log
  coordinates/tokens. Query-time retention is explicit, with physical cleanup lag.

Acceptance: independent shortest-path oracle, actual PNG/GeoJSON, hierarchy,
archive restart/immutability, traffic/expiry/reroute, private sessions and SSE,
location concurrency/replay/retention, resource lifecycle and real independent
process/network faults. Report measured scope. Licensed worldwide data, turn/transit
rules, ML ETA, streaming/distributed stores, HA, actual regions/CDN and book-scale
capacity remain extensions. Submission follows the user's authorized scope;
requirements/tasks retain the existing local-only convention.
