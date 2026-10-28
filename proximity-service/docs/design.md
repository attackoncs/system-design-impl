# Design: Proximity Service

## Module responsibilities

`geo.py` owns spherical geometry and bounded geohash coverage. `models.py` owns
canonical business records, identifiers, query validation and cursors. `cache.py`
owns isolated, bounded cache values and concurrent loading. `store.py` coordinates
SQLite transactions, owner mutations, versioned reads and snapshot installation.
`publisher.py` owns the daily publication loop. `http.py` contains bounded HTTP
transport, authenticated APIs, regional routing and replica polling; `cli.py`
constructs and closes these resources.

Existing imports of `Cache`, `Error`, `canonical` and business validation from
`store` remain compatible. Neither Store nor its caches require a background
thread; only the primary/replica CLI roles start their respective workers.

## Storage and publication

SQLite WAL stores five tables: `drafts` (ID, owner, canonical JSON, tombstone),
`public` (published ID and JSON), `geo` (compound key
`precision, cell, shard, id`), `meta`, and `replay`. Every operation owns a fresh
connection and an explicit read transaction or `BEGIN IMMEDIATE` write
transaction. Initialization closes its connection and uses bounded WAL busy
retries. The persisted shard count survives restart. Existing databases retain
their tables and published head; added metadata tracks draft revision and content
hash. SQLite draft triggers also account for direct maintenance writes.

Publication takes one consistent read of drafts and head metadata. Canonical JSON,
geohash prefixes at all six precision levels and the content hash are constructed
outside the SQLite writer transaction. Installation then checks the captured
head, draft revision and target UTC day. A changed source causes a bounded retry;
three races return a retryable 503 instead of installing stale data. The final
writer transaction replaces public/index rows and advances the generation
atomically. Failed construction or installation leaves the old generation
intact. Bulk installation still holds the SQLite writer and requires memory
proportional to the dataset; this is not an incremental or zero-cost swap.

Existing WAL readers retain their complete previous generation until their read
transaction ends. Public search/detail calls do not wait behind an active
publisher in the same Store; they can continue reading the previous head. Each
response binds metadata, candidates and hydrated objects to one read transaction.
If no publisher is active, a due read can still perform on-demand publication.
A local publication lock avoids duplicate builds within one Store; durable head
and revision fences protect against other Store instances/processes.

The primary CLI starts `DailyPublisher` after binding HTTP. It checks UTC day
changes without client traffic, at `--publication-interval` seconds (default 1).
A mutation publishes the previous day's drafts before accepting the first change
of a new day, then rechecks the day inside its writer transaction. A clock change
at that boundary returns a retryable error. Admin force publication deliberately
makes current drafts visible early. A standalone Store has the same demand
fallback; a library caller must start DailyPublisher for idle publication. Stop
interrupts the loop's wait and joins an active build. Failed background attempts
retain the old head, report a generic publication error and retry on the next tick.
This provides next-day eventual publication, not a hard midnight visibility SLO.

Mutations are owner-only; there is no draft-reading API. Replay keys are scoped
to owner and bind method, ID and normalized body. Response and mutation commit
together. Records expire after seven days and are pruned on writes, so expired
keys may create another business. Public detail, update and deletion all use the
same delayed-visibility generation.

## Caches and query execution

Candidates are cached by lineage, version, precision and cell; business details
by lineage, version and ID. Exact user coordinates are not cache keys. Old cache
entries cannot appear in a new generation, and TTL/LRU bounds eventually reclaim
them. Each cache is process-local, with monotonic TTL and entry-count capacity.
64 fixed `RLock` stripes coalesce same-key cold loads without growing a lock map;
colliding cold keys can still wait on one stripe. The global LRU lock protects
only lookup, accounting and insertion. Loaders run outside it, and warm hits do
not acquire a loading stripe. TTL starts after successful loading; errors do not
poison the cache. Deep copies isolate mutable values supplied by loaders and
returned to callers. Capacity zero disables retention. Store's detail cache uses
immutable canonical JSON text; detail/search parse fresh dictionaries after the
cache lookup. This preserves nested-object isolation without two dictionary deep
copies on each cold load.

Search deduplicates IDs across covering cells. On the first detail-cache miss,
it fetches candidate bodies in SQL batches of 256, rather than issuing a query
per missing business. The request-local hydration map also retains raw JSON text.
Category and opening-time filters run on freshly parsed records,
then spherical distance removes bounding-box false positives. Distance and ID
determine order. Paging slices the sorted matches; computing total still requires
filtering and sorting the full candidate set. Detail-cache churn remains possible
when a dense cell exceeds cache capacity. Cache effectiveness must be measured
for the workload, rather than inferred from its presence.

## Geography

Interleaved longitude/latitude base32 geohash starts at precision 6/5/5/4/4 for
500/1000/2000/5000/20000 meters. This follows the chapter's precision table and
final 500m example; the later cache paragraph reverses the 500m/5km lengths.
A conservative spherical bounding box includes every intersecting grid cell.
Longitude intervals split across ±180; a circle touching a pole covers every
longitude. If more than 4096 cells are required, precision is lowered until the
cover is bounded. A fixed 3×3 neighborhood is insufficient near poles.

Final filtering uses a stable great-circle `atan2` formula with mean earth radius
6,371,008.8m and 1 micrometer inclusion tolerance. It remains accurate near
antipodes and validates both locations. Independent unit-vector cross/dot-distance
checks and destination-point coverage tests exercise poles, antimeridian,
thresholds and seeded random cases. These are spherical calculations, rather than
road distance or ellipsoidal geodesics.

Index cells are hashed into configurable logical shards in one SQLite database.
Following the chapter's choice, geohash is the executable indexing backend;
Quadtree and S2 are alternatives discussed in the source chapter, not additional
required engines. Physical shards and a tree backend remain possible extensions.

## Network roles and ownership

Primary owns owner writes and publication. A replica pulls an authenticated JSON
snapshot over HTTP into its own SQLite database, prepares indexes outside the
writer and installs atomically. A replica remains unready until its first valid
snapshot. Later upstream failure retains the last complete generation. The
protocol validates lineage, day, version and normalized records, rejects foreign
lineage or rollback, and rejects different content for the same version/day.
Identical repeated snapshots are no-ops. The content hash preserves the invariant
that a cache key identifies one immutable generation. Export includes published
businesses only, excluding drafts, owner credentials and replay records.

A gateway round-robins separately within each configured region. Only public read
routes are supported. One total deadline covers admission and all attempts; each
attempt receives a share of remaining time. Admission is bounded (default 32
in-flight reads); slots are released only after underlying work actually exits.
Transport errors, 3xx, 5xx or malformed responses can try another configured
replica. A 4xx, including a cursor conflict, is returned directly. Failover never
crosses region boundaries. A response comes from one replica, whose generation
may lag other replicas.

Outbound fetching uses direct HTTP/HTTPS with normal certificate verification.
It follows neither redirects nor environment proxy configuration, preventing
replication credentials from being sent to a redirect target. The JSON body has
a byte limit (128 MiB for snapshots, 16 MiB for public responses), duplicate keys
and non-finite numbers are rejected, and an owned watcher cancels slow
headers/body reads at the total deadline. Replication has one polling thread;
close signals cancellation, interrupts active I/O and joins it.

Inbound HTTP uses a bounded number of handlers (default 32), bounded backlog,
socket timeouts and a total socket deadline (default 10 seconds). Overload returns
503 without creating another handler. Shutdown cancels active socket work and
joins owned handlers/watchers. Requests reject duplicate headers/query values,
Transfer-Encoding, unknown parameters and unexpected bodies. Owner/admin/
replication authentication occurs before reading mutation bodies. Mutation bodies
are limited to 64 KiB. Bearer comparisons use fixed-length hashes/constant-time
comparison, and credentials are not echoed. Replica and gateway roles reject
writes.

Socket deadlines cannot forcibly interrupt OS DNS resolution, SQLite work or
arbitrary CPU code. Work retains its capacity slot until it finishes, and shutdown
may wait for such work. These limits prevent unbounded admission; they do not
promise a hard deadline for every possible system call.

## Validation, privacy and production boundaries

Opening windows use UTC weekday/start-minute/end-minute intervals; overnight
windows must be split across days. Query/cursor validation rejects non-integer
limits, booleans masquerading as integers, malformed or duplicate cursor fields,
non-finite coordinates and timestamp conversions outside supported UTC bounds.
A cursor binds the full query, lineage and generation; changes return 409. It is
an offset token, not a signed authorization mechanism.

Precise user query coordinates are neither persisted nor access-logged; public
business coordinates are stored by design. Regional routing is configuration,
not proof of legal data residency. Production still needs TLS termination, real
identity and owner proof, region-local deployments and privacy controls, Redis
if measurements justify it, incremental/database replication, service discovery,
monitoring, backups, writer failover and physical partition placement. Local
synthetic experiments do not establish cloud HA or the book's 200 million
businesses / 5000 search QPS. See the [chapter review](chapter17-review.md) and
[validation report](../benchmarks/results.md) for evidence and dated measurements.
