# Design: Google Maps reference

## Responsibilities and storage

| Module | Responsibility |
| --- | --- |
| models / catalog / schema | Shared validation, offline canonical graph preparation, compatible SQL schema |
| store / telemetry | Catalog transactions, identity, snapping/matching, atomic reports and traffic |
| cache | Fixed load stripes, bounded LRU and returned-value isolation |
| geo / navigation | Projection/clipping/distance, exact fine-graph search, geometry and directions |
| routing_tiles / render | Classified topology views, viewport URLs, vector/raster generation |
| artifacts | Separate persistent immutable precomputed tile archive |
| tracking | Private active navigation, area invalidation, refresh/CAS and current-state events |
| transport / http | Strict bounded direct I/O, role routing, authentication, admission and shutdown |
| credentials / maintenance / cli | Credential-file ownership and owned process helpers |

SQLite WAL stores graph nodes/edges/addresses, RTree edge boxes, traffic and private
identity/location state. Parsing, coordinate encoding, canonical hashing and
spatial-row preparation happen before acquiring the graph-install writer. Bulk
installation still holds SQLite's single writer; this is not a zero-cost switch.
Graph UUID, hash and catalog metadata advance atomically. Read transactions keep
one complete generation. A remembered graph_versions map rejects modified content
under a UUID even after an intermediate graph/restart. Old edges gain a default
local road_class in place; normalized default-local exports preserve legacy hashes.

Replica snapshots contain canonical public graph/addresses/license and traffic,
not credentials, private reports, replay rows or navigation sessions. Expired
traffic records remain in exported catalogs so identical revisions do not change
payload merely with wall-clock time; route reads ignore expiry. Equal catalog
revisions require equal metadata/content, rollback/foreign lineage conflict and
validated input is owned before slow work. Full installation is atomic. Clock
synchronization, catalog/version retention and incremental replication remain
operational work.

## Search, ETA and hierarchy

Dijkstra minimizes nonnegative length/speed. One SQL read transaction binds the
graph, expiring driving traffic and all lazily loaded adjacency. Walking/cycling
use mode baselines. A request-local node map reuses hydration across candidates
and geometry/direction generation. max_nodes counts the combined visits of all
candidate searches; exhaustion 413 is distinct from unreachable 404. alternatives
repeats exact searches excluding one of the first 64 edges of the best route,
deduplicates and ranks by ETA. This is bounded single-edge avoidance, not complete
K-shortest paths or an ML ranker.

Results include geometry, precision-5 Google encoded polyline, per-edge distance/
duration and simple depart/continue/turn/U-turn instructions. Headings use spherical
initial bearings, including antimeridian wrapping. Directions are geometric hints;
turn prohibitions, lane/signals and road accessibility rules are not modeled.
Fixture addresses use casefolded exact lookup; coordinate endpoints snap within
1000m. There is no worldwide geocoding/interpolation database.

Road classes are local (default), arterial or highway. Fine routing tiles at z16
contain all roads, z12 views contain arterial/highway, z8 views contain highway.
They include parent/children, cross-level shared-node portals and source/target
XYZ references. Incoming higher-class roads also identify entrances. Each export
is bounded to 5000 nodes/edges by default; SQL limits avoid fetching a whole graph
before detecting overflow. These are classified topology views, not a fabricated
larger copy of every local road. Search deliberately remains exact on the fine
graph; no unproven hierarchical shortcut or acceleration is used.

## Locations and traffic

Credentials are hashed. Reports are normalized into owned scalar records before
waiting for the writer; a batch of 1..100 commits together. Server time is sampled
after writer acquisition, so lock waits cannot extend the admissible timestamp
window. Event IDs are per-user replay keys; different normalized payload under an
existing ID conflicts. Sequence and report time must both advance for current
position; late records stay in receipt-time-retained history without regressing it.

Retention is 24h after receipt, independent of the older client observation time.
Ingestion prunes only that user's expired state; global pruning uses indexed owned
maintenance rather than scanning all users on every report. Query-time filtering
is immediate; physical deletion can lag due to work/downtime and does not certify
legal erasure of disks/backups. Reused expired IDs may be accepted again.

Matching uses RTree candidates and a local tangent approximation within 100m.
Segment longitude is unwrapped relative to the first endpoint, avoiding a
false dateline road through Greenwich. Heading must be unambiguous and within
60 degrees. Increasing progress on the same directed edge at 1..120s separation
and 0.5..50m/s yields a sample only when observations are recent enough for live
traffic. Latest received sample per user (row-order ties resolved) in a 5-minute
window is averaged after at least two distinct users. Administrator closures
remain closed until expiry; aggregate cannot silently reopen them. Aggregation
is explicit SQL processing, without Kafka, Cassandra, HMM or learned prediction.

## Adaptive navigation and delivery

NavigationTracker is primary-only. Authenticated owner sessions store route
options/result, revision, expiry and a containing ancestor tile in private SQL.
Defaults are 128 sessions, TTL 900s (maximum 3600), refresh check 1s. Traffic changes
produce affected edge boxes; the containing area filters potentially impacted
sessions, and a graph change/large change set forces refresh. A periodic full
refresh (default 30s) also catches newly faster alternatives outside the current
route. The spatial filter is invalidation, not a shortest-path shortcut.

Pathfinding occurs outside the navigation write transaction. Installation compares
session revision/expiry, preserving concurrent position updates or removals. A
position command changes origin and recalculates; DELETE removes the private
session. Unreachable/budget errors become current route state rather than an
invented route. Exact acceptance fences and failure recovery are exercised in
[tests](../tests/). The background helper records sanitized errors and retries.

Finite SSE pages return the newest route revision or a heartbeat, then close.
after/Last-Event-ID reconnects replace the displayed current state; intermediate
states are coalesced, not durably replayed. A cursor ahead of the session is 409.
Owner checks and TTL apply to read, move, remove and events. Event waits are at
most 5s and occupy at most three quarters of HTTP slots, preserving foreground
capacity. Session lifecycle is private and excluded from catalogs. This is an
executable server push reference, not a full mobile turn-by-turn client, reliable
message broker, multi-host session store or trained ETA model.

## Map artifacts and rendering

WGS84 projects to XYZ Web Mercator with polar clamping and normalized roundoff
handling; z0..21 is supported. Segment clipping handles shared edges and dateline
unwrap. GeoJSON and real 256×256 RGB PNG use the same clipped segments. Tile URL
lookup returns the current UUID, center and up to 8 unique neighbors; longitude
wraps and polar rows clamp. It does not build a complete mobile viewport renderer.

Cold rendering defaults to 5000 candidate edges/10000 clipped segments, returning 413
when exceeded. LRU cache and archive hits avoid rerasterizing. The archive is a
separate SQLite local object-store adapter, keyed by graph UUID/XYZ/format and
bound to the graph hash; payload checksums and 16MiB object limits are checked on
read. Existing immutable content cannot be replaced. Explicit precompute defaults
to 256 coordinates/64MiB of payload per batch, generating both formats before an
atomic publish. A graph change during preparation cannot mix generations; a
complete older generation can remain archived. Use a distinct file from the
catalog. This does not deploy object replication/CDN or precompute the entire world.

Live origin reads validate the active graph before using cached/archived content.
Old UUID URLs return 410 after replacement; copies already held by an external
CDN would remain immutable. ETag conditional reads (including lists/weak validators)
return 304. Archive retention/GC, styling/fonts, compression/MVT and global tile
pipelines remain extensions.

## Concurrency, roles and shutdown

Adjacency and rendered tiles use bounded entry-count caches. Fixed 64 stripes
coalesce cold loads outside the LRU lock; mutable values are isolated. Adjacency
stores per-node immutable JSON text and parses independent edge records, avoiding
repeated nested dictionary copies or copying an entire dense tile on
every node visit. Entry counts do not imply fixed bytes/RSS; large fine tiles and
graph construction still cost memory proportional to the data.

Primary owns writes/tracking. Replica pulls authenticated full catalogs into its
own database and retains the last complete snapshot on failure. Gateway accepts
only public reads, requires X-Region and retries configured same-region nodes on
transport/5xx/invalid replies; 4xx is returned directly. Redirects are not followed.
Direct transport verifies normal HTTPS identities, rejects ambiguous/truncated
framing, limits bodies and applies one budget across admission/attempts. Credentials
are never forwarded to redirect targets. Strict request JSON/framing rejects
repeated fields and nonfinite numbers; private authentication precedes body reads.

HTTP defaults to 32 handlers/10s request socket budget, with bounded response and
request bodies. Replication, tracking and 30s maintenance bind/start ownership is
explicit: helpers start only after server bind, HTTP closes/drains first, then
helpers join accepted I/O. Cancelled network work retains capacity until actual
exit. DNS, SQLite and CPU calls cannot be forcibly stopped by socket cancellation;
there is no hard shutdown or end-to-end compute deadline. Provisioning fsyncs an
exclusive new file before SQL account creation and cleans only a matching new file
on ordinary failure; this is not a filesystem/SQL atomic crash protocol.

## Evidence and remaining scope

[Validation](../benchmarks/results.md) and [chapter review](chapter19-review.md)
record current tests/measurements, distinct real-process and injected faults,
and code-review-graph navigation limits. Synthetic roads have no real navigation
safety/completeness claim. Production licensed data, turn/transit rules, learned
traffic forecasts, physical sharding, streaming pipelines, consensus discovery,
write failover, TLS/identity, actual regions/CDN, backups and sustained book-scale
load acceptance remain separate work.
