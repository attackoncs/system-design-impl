# Maps System (chapter 19)

Executable reference for directed road routing/ETA, batched location reports,
traffic aggregation, lazy routing tiles and versioned GeoJSON/PNG maps.
[中文](README_CN.md). The road fixture is synthetic CC0 data, not real directions.

## Run

Python 3.10+, standard library only (including SQLite RTree). From this directory:

```powershell
python -m unittest discover -s tests -v
python examples/network_demo.py
python tools/benchmark.py --output .runtime/benchmark.json
```

The demo starts independent primary, replica and gateway processes. Two separate
SQLite files hold public catalogs. It computes a route, closes a road, verifies
replanning over HTTP, fetches actual PNG/GeoJSON, stops primary and verifies the
gateway still reads the replica. Owned processes stop on exit; diagnostic files
remain under ignored `.runtime/lab-<uuid>/`. No external accounts are needed.

Persistent operation, set `$env:PYTHONPATH = 'src'` in each terminal:

```powershell
python -m maps_service.cli import --graph examples/roads.json
python -m maps_service.cli provision --user admin --admin --token-file .runtime/admin.token
python -m maps_service.cli provision --user alice --token-file .runtime/alice.token
python -m maps_service.cli serve --port 8095
```

Then in another terminal:

```powershell
python -m maps_service.cli replica --db .runtime/replica.sqlite --port 8096 --upstream http://127.0.0.1:8095 --token-file .runtime/admin.token
python -m maps_service.cli gateway --port 8097 --regions examples/regions.json
```

Run each server command in its own terminal. Keep private token files out of Git;
the default bind is localhost. TLS and production identity are external extensions.
Optional package install in a virtual environment: `python -m pip install -e .`.

## APIs

| Method/path | Contract |
| --- | --- |
| POST /v1/route | origin, destination, optional mode/avoid_tolls/alternatives/max_nodes |
| GET /v1/geocode?address=Central | Exact casefolded fixture address or node ID |
| GET /v1/reverse?latitude=0&longitude=0 | Nearest fixture node, max 1000m |
| GET /v1/tiles/{graph}/{z}/{x}/{y}.geojson | Versioned clipped GeoJSON |
| GET /v1/tiles/{graph}/{z}/{x}/{y}.png | Actual 256×256 RGB PNG |
| GET /v1/routing-tiles/{graph}/{x}/{y} | z16 adjacency and cross-tile target references |
| POST /v1/locations | Bearer; reports batch, 1..100 items |
| GET /v1/locations | Bearer; most recent 100 self-only events within 24h |
| POST /internal/traffic | Admin Bearer; records, optional ttl (max 3600s) |
| POST /internal/aggregate | Admin Bearer; optional min_users (at least 2) |
| POST /internal/graph | Admin Bearer; atomic graph replacement |
| GET /internal/catalog | Admin Bearer; public graph/traffic export only |
| GET /health | Readiness, graph UUID/hash, catalog/traffic versions and role |

Route origins/destinations are node/address strings or `{latitude,longitude}`.
Modes: driving (default), walking, cycling. alternatives=1..3 uses bounded
single-edge-avoidance candidates, not complete K-shortest paths. max_nodes limits
total visited nodes across candidate searches (default 50000). Search exhaustion
returns 413; unreachable returns 404. Costs are length/speed, not learned ETAs.
No turn restrictions, signals or transit schedules are modeled.

```powershell
$body = @{ origin='West Station'; destination='East Station'; alternatives=2 } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8095/v1/route -Method Post -ContentType application/json -Body $body
```

Each location has event_id, sequence, timestamp (Unix seconds), latitude,
longitude, optional mode and heading (degrees clockwise from north). Replay IDs
are scoped to user; changed payload returns 409 and rolls the entire batch back.
Late sequence/time pairs never regress current position. Timestamps must be within
24h past and 30s future. Raw events are logically retained 24h and physically
pruned on ingestion / every 30s while primary runs. At-rest files need protection.
Tokens/query coordinates are not access-logged. Expired event IDs can be reused.

Map matching is a short-segment tangent approximation within 100m. Only
unambiguous heading-matched driving reports with increasing progress on the same
edge, 1..120s apart and speeds 0.5..50m/s, generate samples. Aggregation uses the
latest received sample per distinct user in a five-minute window; at least two
users are required. It is an explicit operation, not a Kafka/ML pipeline. Admin
traffic overrides have edge/speed_mps/closed fields and expiry. Closures and live
speeds apply to driving; walking/cycling retain baselines. Request /route again
to replan; no mobile background navigation client is supplied.

## Cache, replication and limits

XYZ supports z0..21, longitude x wrap and polar clamping. Tile URLs include a
graph UUID, ETag and immutable cache headers; If-None-Match returns 304. After
graph replacement the old origin URL returns 410. UUID/hash validation prevents
replicas from accepting modified content under an existing graph UUID. A bounded
LRU caches tile payloads and source-tile adjacency. Cache bounds are entry counts,
not a fixed byte budget; dense tiles can be expensive.

Replicas atomically install authenticated full public catalogs, excluding tokens,
user locations and replay records. Default polling is one second; failures retain
the last complete snapshot. Regional gateways require X-Region (e.g. local), retry
only public reads on transport/5xx errors and do not cross regions. This is real
HTTP replication with separate databases, not writer failover or geo deployment.

[Validation](benchmarks/results.md): 14 passing tests, 225 shortest-path oracle
comparisons, concurrent graph swaps and actual process/network recovery. A 900-node
synthetic grid produced 60 error-free HTTP requests at 72.17 mixed QPS in a small
local run. No book-scale, cloud/CDN, ML ETA or production navigation claim.
See [design](docs/design.md).
