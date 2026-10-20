# Design: Google Maps reference

## Versioned graph and spatial data
SQLite WAL holds normalized nodes/edges/addresses, SQLite RTree edge bounding
boxes, traffic, users, location events and configuration. Atomic imports build a
fresh graph then swap graph UUID and catalog revision in the same transaction.
Old readers keep their snapshot. Graph cache keys contain graph UUID; traffic
weights are read under the same read transaction, so one route cannot mix versions.
Adjacency is grouped by source-node XYZ routing tile (z=16), including references
to target nodes in other tiles. A bounded LRU loads only tiles reached by search.
This is partitioned lazy routing, not contraction hierarchies or global A*.

## Routing and ETA
Dijkstra minimizes nonnegative travel time. Speeds are per edge/mode in meters
per second; live driving overrides/closures expire. The baseline model is
length/speed, with no turn penalties, signals, scheduled public transit or learned
traffic forecast. Candidates rerun shortest path with one edge of the best route
avoided, deduplicate and rank results. This bounded heuristic is not a complete
K-shortest-path algorithm. Explicit /route requests compute a fresh route for
replanning. Address resolution is exact casefolded fixture lookup; coordinate
origins/destinations use bounded nearest-node snapping.

## Location/traffic pipeline
Owner credentials are provisioned locally and stored hashed. Batches are limited
to 100 reports. Each (user,event ID) stores a payload hash and result; a different
payload under the same ID conflicts. The whole batch commits or rolls back.
Accepted sequence/timestamp pairs advance current position monotonically; late
events stay in retained history but do not alter current traffic sampling state.
Nearest-segment matching uses a local tangent-plane approximation (maximum 100m),
not HMM map matching. Heading selects the directed edge and must agree within
60 degrees; absent/ambiguous heading still stores the report but cannot produce
a speed sample. Only increasing fractions on the same directed edge yield
speed samples; unrealistic speeds and time gaps are rejected. Distinct users
and a 5-minute window gate mean-speed traffic updates. Manual admin traffic and
explicit aggregate operations bump catalog revisions. This SQL pipeline replaces
Kafka/Cassandra for a local reference; no simulated Kafka deployment is claimed.

## Tiles and network roles
WGS84 is projected to XYZ Web Mercator with polar clamping. Segment clipping in
normalized projected coordinates handles shared edges and antimeridian unwrap.
GeoJSON retains geographic coordinates; PNG uses a standard-library line rasterizer
and zlib PNG encoder. Both are real outputs from the same clipped segments.
URLs include graph UUID, z/x/y and format, with content ETag and immutable cache
headers. Old origin graph URLs return 410 after replacement; existing CDN copies
would remain valid because content at a UUID URL never changes. No CDN is deployed.

Primary owns graph import, reports and traffic. Independent read-only replicas
pull authenticated full public catalogs (graph, addresses, active traffic) into
separate SQLite files. Installation is atomic, lineage/version checks reject
rollback or foreign source. Private location/identity tables are excluded. A
normalized SHA-256 graph checksum rejects changed roads under an existing UUID
and preserves the imported dataset license across copies. A
gateway forwards public geocode/route/tile reads to configured same-region peers,
retrying transport and 5xx errors. Replicas serve the last complete snapshot while
the primary is unavailable. Snapshot staleness is visible in health/route results.
Local region configuration is not geographic deployment; full-copy replication
is not incremental database replication or primary failover.

## Scope boundaries
Bounding boxes use RTree for candidates; exact matching is approximate on short
segments. Synthetic roads are for deterministic engineering tests, not real travel.
No navigation safety/road completeness guarantee. Production requires licensed
road ingestion, turn restrictions, hierarchical routing, accurate map matching,
prediction models, scalable streams/history, HTTPS, identity, region-local storage,
HA, backups, map styling/fonts and real load/operational validation. Retention is
logical at query time, with opportunistic physical pruning while active.
