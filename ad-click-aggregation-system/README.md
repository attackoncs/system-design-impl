# Ad Click Event Aggregation — Chapter 22

A standard-library Python reference implementation with an actual six-process HTTP pipeline: durable partitioned raw log → four independent event-time aggregators → global query/reduce API. Supports duplicates, late corrections, exact dimension filters, salted hot ads, checkpoint recovery, offline replay and independent reconciliation.

Requires Python 3.10+. No external Python dependencies or external services are needed. This chapter uses its own single-owner SQLite raw-log transport; it does not claim replicated broker HA or the book's billion-event scale.

## Run and verify

From this directory:

```powershell
$env:PYTHONPATH = 'src'
python examples/network_demo.py
python -m unittest discover -s tests -v
python tools/benchmark.py --output benchmarks/results.json
```

The demo and network tests allocate unused localhost ports, create private credentials, launch their own six processes, and stop only those processes on exit. Logs/databases remain under ignored `.runtime/lab-*` for inspection. On environments with a restricted system temp directory, create `.runtime/tmp` and set `TEMP`/`TMP` to that absolute path before testing.

Measured results and boundaries: [benchmarks/results.md](benchmarks/results.md). The network suite includes an actual process exit **inside an uncommitted aggregation transaction**, worker restart, ingress restart, and a Top N counterexample that cannot be solved by merging only local winners.

## Start services manually

```powershell
$env:PYTHONPATH = 'src'
python tools/init_config.py
# Run each command in its own terminal with the same PYTHONPATH:
python -m ad_click.cli ingress --config .runtime/secrets.json --port 9820
python -m ad_click.cli worker --config .runtime/secrets.json --partition 0 --port 9822
python -m ad_click.cli worker --config .runtime/secrets.json --partition 1 --port 9823
python -m ad_click.cli worker --config .runtime/secrets.json --partition 2 --port 9824
python -m ad_click.cli worker --config .runtime/secrets.json --partition 3 --port 9825
python -m ad_click.cli api --config .runtime/secrets.json --port 9821
```

The private generated configuration contains distinct `write_token`, `read_token`, `internal_token`, `admin_token`, immutable `partitions`, `ingress_url`, ordered `worker_urls`, `lateness` (120 seconds), and `max_events` (1,000,000 unique events). Configuration creation refuses overwrite. `.runtime` is ignored; do not publish credentials or databases. All processes default to `127.0.0.1` and share only configuration; each worker has its own database file.

## Ingest and query

Every event requires a stable identity. Different clicks by one user must have different event IDs.

```json
{"event_id":"click-001","ad_id":"ad-1","click_timestamp":1700000001,"user_id":"u-1","ip":"127.0.0.1","country":"US"}
```

HTTP `POST /events` on ingress accepts `{"events":[...]}` with a write bearer token, 1–200 events per request. A repeated ID with identical normalized content returns the original partition/sequence and `duplicate=true`; conflicting content returns 409 and rolls back the whole batch. Future timestamps over 30 seconds are rejected. New unique events over capacity return 429. Durable acceptance does not mean aggregation has caught up.

Append one event JSON object per line to a source file, including a final newline, then run:

```powershell
python -m ad_click.cli ingest-file --config .runtime/secrets.json --source clicks.jsonl --checkpoint .runtime/clicks.cursor.json
```

Rerun to ingest newly appended complete records. Checkpoints store byte offset and prefix checksum after remote acknowledgement; incomplete final records wait. Prefix changes/truncation fail instead of silently skipping data. Rotation needs a new checkpoint, while stable event IDs prevent recounting replayed records. One process must own each checkpoint.

The query API accepts a read bearer token:

| API | Meaning |
| --- | --- |
| `POST /query` | JSON query below; exact global Top N, or count when `ad_id` is supplied |
| `GET /v1/ads/popular_ads?window=5&count=100&country=US` | Top N in the last M complete minutes |
| `GET /v1/ads/ad-1/aggregated_count?window=5&user_id=u-1` | Advertisement count with optional AND filters |
| `POST /admin/reconcile` | Admin only; compare online dimensions with independent raw batch math |
| `GET /health` | Minimal public role/readiness status |

```json
{"from":1699999980,"to":1700000280,"count":100,"filters":{"country":"US","ip":"127.0.0.1"}}
```

`from`/`to` are epoch seconds aligned to minutes; ranges are half-open `[from,to)` and at most 1440 minutes. Alternatively use `window` (default 1), ending at the latest complete minute. `count` defaults to 100 and is bounded to 1000. `filters` supports `user_id`, `ip`, `country` intersections. Add `ad_id` for a single advertisement's `count`, including zero for absent ads. Ties sort by ad ID. Candidate budget is 10,000 ads across all partitions; exceeding it returns 422 and requires narrower queries.

Example Python client (with service configuration kept private):

```python
import json
from pathlib import Path
from ad_click.common import http_json
c = json.loads(Path('.runtime/secrets.json').read_text())
print(http_json('http://127.0.0.1:9821/query', c['read_token'],
                {'window': 5, 'count': 10, 'filters': {'country': 'US'}}))
```

## Semantics and operations

- Event-time minute buckets remain correct under out-of-order delivery. Watermarks advance monotonically using max event time/recorded acceptance time minus allowed lateness, and idle caught-up workers advance from wall clock. Valid late records always correct old buckets. `closed=true` is a watermark observation, **not immutable finality**.
- Each result includes `lineage`, `checkpoints`, `lag`, `watermark`, `closed` and `late_corrections_allowed`. It combines per-partition snapshots, not a single cross-partition transaction. Checkpoint vectors identify revisions. Lag is an estimate from separate snapshots.
- Salted partitioning spreads one hot ad across workers. Global Top N first merges all candidate totals; local top-N truncation would be wrong. Any required worker/source failure or missing topology returns 503; partial results are not successful responses.
- Counts, dedup state and checkpoint commit atomically in each worker. A crash replays safely from the prior transaction. Queue lineage and partition assignment are checked; partition count cannot change in place.
- Ingress `GET /internal/status` exposes raw capacity and partition heads. Worker `GET /internal/status` exposes position, bucket rows, watermark, correction count, last poll and error. These and `/internal/log`, `/internal/totals`, `/internal/audit` require internal/admin credentials.

```powershell
python -m ad_click.cli reconcile --config .runtime/secrets.json
python -m ad_click.cli rebuild --config .runtime/secrets.json --partition 0 --output .runtime/rebuilt-0.sqlite
```

Reconciliation independently re-counts raw dimensions through each worker's captured checkpoint and hashes them against an atomic online audit. A mismatch returns CLI exit status 1. Rebuild reuses the stream processor into a new database through a captured raw head; it refuses to overwrite files. Neither command automatically replaces online data. Administrative batch work loads dimensions into memory and is intended for this bounded reference workload.

## Limits

Single raw-log owner and per-worker SQLite files are not replicated or automatically failed over. Recovery tests retain local disks. Worker placement and partition count are static; no online resharding is supplied. Raw records, dimensions and dedup keys have no automatic retention cleanup; capacity requires explicit migration, not deletion that would break historical deduplication. Full dimension storage can approach one row per event. There is no fraud detection, financial settlement, TLS, tenant isolation, automatic privacy lifecycle or production distributed batch engine. Deploy only on trusted local networks; production requires those separate designs. See [design](docs/design.md) for precise invariants and tradeoffs.
