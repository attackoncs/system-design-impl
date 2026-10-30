# Maps System (chapter 19)

<!-- LEARNING_GUIDE_START -->
## Code reading guide

[Learn through a concrete example: architecture, state changes and annotated source (Chinese)](docs/learning-guide_cn.md).
<!-- LEARNING_GUIDE_END -->

Executable reference for directed routing/ETA, batched locations, traffic,
classified routing tiles and versioned GeoJSON/PNG maps. Includes precomputed
persistent tiles and private navigation sessions with adaptive route updates.
[中文](README_CN.md). Roads are synthetic CC0 fixtures, not real directions.

## Run

Python 3.10+, standard library only (including SQLite RTree). From this directory:

```powershell
python -m unittest discover -s tests -v
python examples/network_demo.py
python examples/adaptive_demo.py
python tools/benchmark.py --output .runtime/benchmark.json
```

The demo uses independent primary, replica and gateway processes and separate
catalog databases, exercising actual HTTP route/traffic/tile recovery. Owned
processes stop on exit; diagnostic files remain in ignored `.runtime/lab-<uuid>/`.
The adaptive demo separately runs CLI precomputation and actual private SSE
replanning over HTTP. No external account is required. Optional install: `python -m pip install -e .`.

Set `$env:PYTHONPATH = 'src'` in each terminal for persistent commands:

```powershell
python -m maps_service.cli import --graph examples/roads.json
python -m maps_service.cli provision --user admin --admin --token-file .runtime/admin.token
python -m maps_service.cli provision --user alice --token-file .runtime/alice.token
python -m maps_service.cli precompute --tiles examples/tiles.json --tile-archive .runtime/tiles.sqlite
python -m maps_service.cli serve --port 8095 --tile-archive .runtime/tiles.sqlite
```

Run replica and gateway in separate terminals:

```powershell
python -m maps_service.cli replica --db .runtime/replica.sqlite --port 8096 --upstream http://127.0.0.1:8095 --token-file .runtime/admin.token
python -m maps_service.cli gateway --port 8097 --regions examples/regions.json
```

The tile archive must be separate from the catalog. A replica may read its own
precomputed archive with --tile-archive; catalog replication does not transfer
archive files. Provisioning exclusively writes/fsyncs a new credential before SQL
account creation and cleans its matching new file on ordinary failure. Protect
credential files and databases. Defaults bind localhost; production TLS/identity
are deployment work.

## Public APIs

| Method/path | Contract |
| --- | --- |
| POST /v1/route | origin/destination; optional mode, avoid_tolls, alternatives, max_nodes |
| GET /v1/geocode?address=Central | Exact normalized fixture address or node ID |
| GET /v1/reverse?latitude=0&longitude=0 | Nearest fixture node within 1000m |
| GET /v1/tile-urls?latitude=0&longitude=0&zoom=14 | Version-pinned center and neighboring tile URLs; optional format=png/geojson |
| GET /v1/tiles/{graph}/{z}/{x}/{y}.png | Actual 256×256 RGB PNG; .geojson returns clipped vector features |
| GET /v1/routing-tiles/{graph}/{z}/{x}/{y} | z16 all roads, z12 arterial/highway, z8 highway; parent/children/entrance references |
| GET /v1/routing-tiles/{graph}/{x}/{y} | Compatible z16 form |
| POST/GET /v1/locations | Bearer; reports batch of 1..100 / latest 100 self-only retained events |
| POST /v1/navigation | Bearer, primary only; route options plus optional ttl; create private session |
| GET/DELETE /v1/navigation/{id} | Owner-only current state / remove session |
| POST /v1/navigation/{id}/position | Owner-only {origin: node/address/coordinate object}; recalculate route |
| GET /v1/navigation/{id}/events?after=N | Owner-only finite SSE page; Last-Event-ID resumes newest route state |
| GET /health | Catalog/traffic versions, role, readiness and attached helper diagnostics |

Admin Bearer APIs: POST /internal/graph replaces the graph; POST
/internal/traffic accepts records and optional ttl; POST /internal/aggregate
accepts optional min_users; GET /internal/catalog exports only the public catalog.

Route endpoints accept node/address strings or `{latitude,longitude}`. Driving,
walking and cycling are supported; no scheduled transit. A route contains nodes,
edges, geometry, precision-5 encoded polyline and structured steps with per-edge
distance/duration and simple direction instructions. These are geometric hints,
without turn restrictions, signals or lane guidance. Dijkstra uses one consistent
SQL graph/traffic snapshot. alternatives=1..3 uses bounded single-edge avoidance,
not complete K-shortest paths; max_nodes defaults to 50000 and counts all candidate
searches. Budget exhaustion is 413; unreachable is 404. ETA is length/speed,
not learned prediction. Search remains exact on fine tiles, without claiming
hierarchical acceleration.

## Locations and live navigation

Reports contain event_id, sequence, Unix timestamp, latitude/longitude, optional
mode and heading clockwise from north. A durable per-user ID replay is identical
on retry; conflicting reuse rolls back the batch. Late reports do not regress
current state. Time admissibility (24h past / 30s future) is checked after the
SQLite writer is acquired. Receipt-time retention is 24h; history is self-only.
Ingestion prunes that user's expired data; owned maintenance sweeps globally every
30s while running. Cleanup may lag during downtime. Coordinates/tokens are not
access-logged, and private data is excluded from replicas.

Map matching is a short-segment tangent approximation within 100m. Only
unambiguous heading-matched driving reports moving forward on the same edge,
1..120s apart at 0.5..50m/s and recent enough for the live window produce samples.
Aggregation takes the latest received sample per distinct user within five
minutes, requires at least two users and preserves unexpired administrator
closures. It is explicit SQL processing, not a Kafka/ML deployment.

Navigation sessions default to TTL 900s and a capacity of 128 per primary. A 1s
background check uses affected traffic boxes and a containing parent tile as an
invalidation prefilter; periodic full refresh also discovers faster new routes.
Session results are updated with compare-and-swap, not by holding a writer during
pathfinding. SSE returns the newest route state or a heartbeat and closes; clients
reconnect with Last-Event-ID and replace the displayed state. Intermediate states
are coalesced, not a durable event replay. Events wait at most 5s and use at most
three quarters of HTTP handler capacity. TTL does not provide multi-host session HA.

## Tiles, engineering and evidence

XYZ supports z0..21, longitude wrapping and polar clamping. Cold rendering is
bounded by 5000 candidate edges /10000 clipped segments; dense tiles can return 413
and require higher zoom or offline production preparation. Layer exports default
to 5000 nodes/edges. TileArchive stores immutable PNG/GeoJSON with graph identity
and checksum: default precompute batch 256 tiles /64MiB and 16MiB per object.
Versioned URLs, ETags and 304 make CDN integration possible. The active origin
returns 410 for old graph URLs; retained archive generations are not origin history.
No cloud object store or CDN is deployed.

Cache modules coalesce cold loads outside the LRU lock and isolate mutable values;
adjacency caches immutable per-node JSON text and parses independent edge records
for each caller, avoiding repeated nested dictionary copies. Cache capacities are entry
counts, not RSS limits. HTTP defaults to 32 handlers and a 10s socket request budget;
--concurrency/--request-timeout configure these. --navigation-interval and
--max-navigation-sessions configure the owned navigation helper. Replication,
maintenance and navigation start after bind and are drained on close. Strict
JSON/framing, authentication before private body reads, bounded direct transport
and same-region failover reject redirects and never forward replica credentials
to a redirect target. DNS/SQLite/CPU work cannot be forcibly stopped by socket timeouts.

[Current validation](benchmarks/results.md) records final tests and dated
[raw measurement](benchmarks/review-2026-10-25.json). The original 2026-10-20 run had
14 tests and 72.17 mixed QPS; these are historical observations. See the
[chapter review](docs/chapter19-review.md) and [design](docs/design.md) for coverage
and limits. Licensed road ingestion, ML ETA, production transit/turn rules,
physical sharding, HA, regions and book-scale traffic remain extensions.
