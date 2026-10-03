# Search Autocomplete System

Chapter 14 reference implementation: completed-search logs, offline aggregation,
atomic snapshots, cached top-five tries, filtering, balanced shards and HTTP replicas.
Standard library runtime, Python 3.9+. [中文说明](README_CN.md) · [Design](docs/design.md).

## Run

```bash
pip install -e ".[dev]"
pytest -q
python examples/basic.py
python examples/network_demo.py
```

The network demo owns three shard processes and one coordinator, kills one replica,
verifies the same global ranking, then verifies immediate removal. All temporary
processes/data are cleaned up. It sends no analytics to external services.

```bash
python -m search_autocomplete.cli --db demo.sqlite3 record event-1 "twitch" --at 100
python -m search_autocomplete.cli --db demo.sqlite3 build --start 0 --end 200 --shards 2
python -m search_autocomplete.cli --db demo.sqlite3 query tw
python -m search_autocomplete.cli --db demo.sqlite3 block twitch
python -m search_autocomplete.cli --db demo.sqlite3 unblock twitch
```

Build without time arguments uses the preceding seven days; schedule that command
weekly through your deployment scheduler. Analytics events are completed searches,
not every suggestion request. Explicit event timestamps use UTC epoch seconds.
Retry an event with the same ID/query/time; omitted timestamps reuse the first event's
time. Conflicting reuse is rejected. Windows examples also accept PowerShell commands.

## HTTP shards

Publish at least two distinct queries before requesting two shards. Actual shard
count is bounded by distinct query count. Run each node in a separate terminal:

```bash
python -m search_autocomplete.cli --db demo.sqlite3 node --shard 0 --port 8100
python -m search_autocomplete.cli --db demo.sqlite3 node --shard 0 --port 8101
python -m search_autocomplete.cli --db demo.sqlite3 node --shard 1 --port 8102
```

Create `replicas.json` using the current snapshot's shard IDs:

```json
{"0":["http://127.0.0.1:8100","http://127.0.0.1:8101"],"1":["http://127.0.0.1:8102"]}
```

```bash
python -m search_autocomplete.cli --db demo.sqlite3 coordinator --replicas replicas.json --port 8080
```

GET `http://127.0.0.1:8080/suggest?prefix=tw`. Responses include version, filter
revision and query/frequency suggestions. Every relevant shard must be available;
missing shards or incompatible versions return 503 rather than partial rankings.
Invalid input returns 400. Prefixes are normalized English letters/spaces, at most
50 characters; trailing prefix spaces remain significant. Empty prefixes return no
suggestions after a snapshot is published. Scores descend, lexical order breaks ties.

Blocking affects the next request, including cached responses; a request already
in flight can complete under its captured revision. Later builds exclude blocked
terms. Unblocking a physically excluded term restores it after the next build.
Responses use `Cache-Control: no-store` so browser caches cannot bypass removals.
This intentionally omits the chapter's one-hour browser cache optimization.

## Validation and limits

30 tests passed, including actual HTTP merge/fallback and independent-process
termination, durable restart, atomic build failure, event retries and filtering.
Run `python tools/benchmark.py --output .runtime/benchmark.json` after creating
the output directory. The saved [latency report](benchmarks/results.md) measures
10,000 queries, 10,000 local lookups and 500 actual HTTP requests.

SQLite log/snapshot/metadata storage is shared on one host. The query protocol
supports separate replica processes, but this is not multi-host storage HA or a
consensus metadata service. Keep replicas on the same published snapshot lineage;
generation IDs are local to that database. Top-five lookup normally avoids subtree
scans; immediate blocked-term refill can scan a subtree until a filtered rebuild.
Caches/HTTP handlers are bounded; historical events and snapshots need an operator
retention policy. Scaling targets of 10 million daily users / 48,000 peak QPS are
not established by the local sequential benchmark. Live trending, Unicode/country
ranking, authentication and TLS deployment are extension points.
