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
python -m proximity.cli primary --db .runtime/primary.sqlite --port 8080 --tokens examples/tokens.demo.json --publication-interval 1
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
Normal publication is next UTC day. The primary CLI owns a background
`DailyPublisher`, checking the day without incoming requests; the default check
interval is one second (`--publication-interval`, 1..86400 seconds). Publication
and replication take additional time, so this is not an exact-midnight deadline.
Today's first mutation freezes yesterday's drafts before accepting today's change.
Library-only `Store` use also retains publication on demand; start a
`DailyPublisher` explicitly if it must publish without traffic.

An external scheduler can alternatively use the bodyless one-shot command:

```powershell
python -m proximity.cli publish --db .runtime/primary.sqlite
```

`--force` or the admin endpoint deliberately publishes early. Shutdown stops the
publisher and replication polling, cancels active network reads and joins owned
threads; in-progress database publication is allowed to finish.

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
| GET /health | Role, version, UTC snapshot day, readiness and replication/publication error when the corresponding worker is attached |

Business JSON requires name/latitude/longitude, with optional address, category
(default other) and hours (default empty). Coordinates must be finite JSON
numbers. UTC weekly windows look like `[{"day":1,"start":600,"end":720}]` for
Tuesday 10:00–12:00. Split overnight windows across days; empty hours means
unknown and excludes open_at matches. open_at is an ISO timestamp with timezone.
Public details and search both retain the old record until publication. Replay
expires after seven days and is pruned on subsequent writes; reusing an expired
key can create another business.

Search returns total, businesses, next_cursor, version, snapshot_day and cell/
candidate diagnostics. Stable spherical great-circle distance filters candidates;
order is distance then ID. Radius never expands. limit is 1..1000 (default 100).
Follow next_cursor with the identical query to retrieve all matches. A changed
query or published generation returns 409; restart from the first page. Cursors
are strictly validated, but are not signed credentials or authorization tokens.

Gateway reads require X-Region and only fail over within that configured region.
Transport errors, redirects, 5xx and invalid responses may try another configured
replica; 4xx, including cursor conflicts, are returned directly. Redirect targets
are never followed. A bounded admission limit and one total request budget cover
admission and replica attempts. Replica lag remains visible.

HTTP request bodies, response bodies and simultaneous handlers are bounded;
ambiguous framing, repeated fields and non-finite JSON numbers are rejected.
Precise query coordinates are neither persisted nor access-logged. Business
coordinates remain part of public business records.

## Evidence and limits

See the [validation report](benchmarks/results.md) for dated test, geometry,
independent-process and benchmark results, and the
[chapter review](docs/chapter17-review.md) for the requirement mapping and fixes.
The implementation separates geography, record/query validation, cache behavior,
publication and HTTP transport. Cache misses load outside the global LRU lock;
64 fixed lock stripes coalesce concurrent loads and TTL starts after a successful
load. The generic cache isolates mutable values with deep copies; Store caches
immutable canonical JSON text and parses fresh objects for detail/search, avoiding
repeated dictionary copies while preserving nested-value isolation. Search fetches
cold candidate bodies in SQL batches of 256.

Full-snapshot HTTP replication uses independent SQLite files. Logical shards
remain in one database; region maps are local routing configuration. Caches are
process-local, rather than a deployed Redis cluster. Snapshot/index construction
is prepared outside the SQLite writer, but installing all rows still holds the
writer transaction and uses memory proportional to the dataset. Dense queries
still filter and sort all candidates to compute total. Socket deadlines cannot
forcibly interrupt platform DNS resolution or database/CPU work.

Cloud HA, physical sharding, database WAL replication, TLS termination, production
identity and the book's 200 million businesses / 5000 QPS remain extensions.
See the [design](docs/design.md).
