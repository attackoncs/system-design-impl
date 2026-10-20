# Proximity Service

Chapter 17 executable reference: geohash nearby search, owner CRUD, durable
idempotency, atomic daily snapshots, bounded caches, independent HTTP replicas
and a region-aware read gateway. [中文说明](README_CN.md).

## Run

Python 3.10+, no third-party runtime/test dependencies. From this directory:

```powershell
python -m unittest discover -s tests -v
python examples/cluster_demo.py
python tools/benchmark.py --output .runtime/benchmark.json
python tools/http_benchmark.py --output .runtime/http-benchmark.json
```

The demo starts four independent processes and three separate SQLite databases.
It queries across the antimeridian, stops a replica to exercise failover and stops
the primary to verify retained snapshot reads. It cleans up on exit.

For persistent operation, set `$env:PYTHONPATH = 'src'` in each of four terminals,
then run one command per terminal:

```powershell
python -m proximity.cli primary --db .runtime/primary.sqlite --port 8080 --tokens examples/tokens.demo.json
python -m proximity.cli replica --db .runtime/r1.sqlite --port 8081 --tokens examples/tokens.demo.json --upstream http://127.0.0.1:8080
python -m proximity.cli replica --db .runtime/r2.sqlite --port 8082 --tokens examples/tokens.demo.json --upstream http://127.0.0.1:8080
python -m proximity.cli gateway --port 8083 --regions examples/regions.demo.json
```

Default bind is localhost. Demo tokens are public credentials; supply private
random credentials and TLS for nonlocal use. Do not share databases between roles.
Optional editable install in a virtual environment: `python -m pip install -e .`.

Create a draft and explicitly publish it for demonstration:

```powershell
$headers = @{ Authorization='Bearer alice-demo'; 'Idempotency-Key'='cafe-1' }
$body = @{ name='Dateline Cafe'; latitude=0; longitude=179.999; category='cafe' } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8080/v1/businesses -Method Post -Headers $headers -ContentType application/json -Body $body
Invoke-RestMethod http://127.0.0.1:8080/internal/publish -Method Post -Headers @{ Authorization='Bearer admin-demo' }
Invoke-RestMethod 'http://127.0.0.1:8083/v1/search/nearby?latitude=0&longitude=-179.999&radius=500' -Headers @{ 'X-Region'='local' }
```

Wait for a replica polling interval (default one second) before gateway reads.
Normal publication is next UTC day, triggered by primary reads, mutations or
replica polls. Today's first mutation freezes yesterday's drafts first. With no
traffic there is no midnight daemon; an external scheduler can run:

```powershell
python -m proximity.cli publish --db .runtime/primary.sqlite
```

`--force` or the admin endpoint deliberately publishes early.

## API contract

| Method/path | Behavior |
| --- | --- |
| GET /v1/search/nearby | latitude/longitude required; radius meters 500/1000/2000/5000(default)/20000; optional category, open_at, limit, cursor |
| GET /v1/businesses/{id} | Published detail and version |
| POST /v1/businesses | Create owned draft; Bearer and Idempotency-Key required |
| PUT /v1/businesses/{id} | Replace owned draft; same authentication/replay requirements |
| DELETE /v1/businesses/{id} | Tombstone owned draft; same authentication/replay requirements |
| POST /internal/publish | Admin Bearer; primary only; forced publication |
| GET /internal/snapshot | Replication Bearer; public snapshot export |
| GET /health | Role, version, UTC snapshot day, readiness and replication error |

Business JSON requires name/latitude/longitude, with optional address, category
(default other) and hours (default empty). UTC weekly windows look like
`[{"day":1,"start":600,"end":720}]` for Tuesday 10:00–12:00. Split overnight
windows across days; empty hours means unknown and excludes open_at matches.
open_at is an ISO timestamp with timezone. Public details and search both retain
the old record until publication. Replay expires after seven days and is pruned
on subsequent writes; reusing an expired key can create another business.

Search returns total, businesses, next_cursor, version, snapshot_day and cell/
candidate diagnostics. Mean-earth spherical distance filters all candidates;
order is distance then ID. Radius never expands. limit is 1..1000 (default 100).
Reuse the identical query with cursor. Publication/query change returns 409:
restart from the first page. Gateway reads require X-Region and only fail over
within that configured region on transport/5xx errors. Replica lag is visible.
Precise query coordinates are neither persisted nor access-logged.

## Evidence and limits

[Validation report](benchmarks/results.md): 17 tests pass; 10,000 synthetic
businesses; 120 real HTTP requests with four clients, 29.5 QPS and zero errors.
Full-snapshot HTTP replication uses independent SQLite files. Logical shards
remain in one database; region maps are local routing configuration. Cloud HA,
physical sharding, WAL replication, TLS, production identity and book-scale
5000 QPS remain extensions. See [design](docs/design.md).
