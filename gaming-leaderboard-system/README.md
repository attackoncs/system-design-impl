# Real-time Gaming Leaderboard — Chapter 26

Monthly wins, exact competition ranks, fixed Top 10 and four-row neighbor windows using real hash-sharded Redis sorted sets. SQLite is the authoritative match journal; projections can replay after Redis loss. The lab runs seven processes: one authority, two HTTP projectors and four Redis primary/replica processes.

Python 3.10+, standard-library client code; **a real Redis executable is required**. Redis 8.0.5 was tested. No Redis mock or sibling-project source is imported.

## Run with real Redis

From this directory, point the lab to your installed executable:

```powershell
$env:LB_REDIS_BINARY = 'C:/path/to/redis-server.exe'
python examples/network_demo.py
python -m unittest discover -s tests -v
python tools/benchmark.py --output benchmarks/results.json
```

On Linux, Redis on PATH or `LB_REDIS_BINARY=/path/to/redis-server` works. For Windows with Redis inside WSL, use these **instead of** `LB_REDIS_BINARY`:

```powershell
$env:LB_WSL_DISTRO = 'Ubuntu-26.04'
$env:LB_WSL_REDIS_BINARY = '/path/to/redis-server'
# Only if an unpacked binary needs its own shared libraries:
$env:LB_WSL_REDIS_LIB = '/path/to/libraries'
python examples/network_demo.py
```

Use actual executable/library paths. The lab creates private Redis passwords/namespaces, four owned AOF directories, two projection nodes and an authority. It checks run IDs before stopping Redis and stops only its children. Ignored `.runtime/lab-*` retains data/logs. If system temp access is restricted, create `.runtime/tmp` and set `TEMP`/`TMP` to that absolute directory. No packages are automatically installed or existing Redis services modified. See [validation report](benchmarks/results.md).

## Service configuration

```powershell
$env:PYTHONPATH = 'src'
python tools/init_config.py
# Provision the two Redis primary/replica pairs described in this private config first.
python -m gaming_leaderboard.cli authority --config .runtime/secrets.json --port 9860
python -m gaming_leaderboard.cli projection --config .runtime/secrets.json --shard 0 --port 9861
python -m gaming_leaderboard.cli projection --config .runtime/secrets.json --shard 1 --port 9862
```

Each service command runs in a separate terminal. Config creation refuses overwrite. Fields include separate admin/read/game/internal bearer credentials, Redis password, namespace, authority URL, ordered projection URLs, Redis endpoint pairs and `late_grace=172800`. Default Redis ports are 9863–9866. Config generation does not install/start Redis; the automatic demo/tests provision only their own instances. Set AOF/replication on your own deployment explicitly and keep credentials private. Servers/endpoints bind loopback only.

## HTTP API

| API | Credential / behavior |
| --- | --- |
| `PUT /admin/players/{user}` | Admin, `{name}`; register/update display name and current-month membership |
| `POST /v1/scores` | Trusted game service, `{match_id,winner,players,ended_at}`; one point per unique win |
| `GET /v1/scores?month=YYYY-MM` | Read token; at most ten players, defaults to current UTC month |
| `GET /v1/scores/{user}?month=YYYY-MM&neighbors=4` | Read token; target and at most four display rows per side |
| `GET /v1/seasons/{month}` | Read token; season status, late deadline and optional settlement |
| `POST /admin/close` | Admin, `{month}`; immutable settlement after late deadline |
| `POST /admin/reconcile` | Admin, `{month}`; independent full SQL/Redis score comparison |
| `POST /admin/rebuild` | Admin, `{}`; clear only owned projection keys and replay the journal |

```python
import json, time
from pathlib import Path
from gaming_leaderboard.common import http_json
c = json.loads(Path('.runtime/secrets.json').read_text())
url = c['authority_url']
for user in ['alice', 'bob']:
    http_json(url + '/admin/players/' + user, c['admin_token'], {'name': user}, 'PUT')
result = {'match_id': 'match-001', 'winner': 'alice', 'players': ['alice', 'bob'],
          'ended_at': int(time.time())}
print(http_json(url + '/v1/scores', c['game_token'], result))
print(http_json(url + '/v1/scores', c['read_token']))
```

Only the game credential can submit results; no arbitrary `points` field is accepted. IDs are 1–64 ASCII letters/digits/underscore/hyphen. A match has 2–8 distinct registered participants including its winner. Match ID reuse with normalized identical content returns its original receipt, changed content returns 409. Client retries must reuse the original timestamp and result.

All registered players, including zero-score players, enter the month. Rank is `1 + count(strictly higher scores)`: `1,2,2,4`. Ties display by user ID ascending. Top 10 returns exactly ten rows when available and does not expand ties. `position` is the unique display position; neighbors refer to those positions, not whole tied groups. Profiles are looked up from authority.

## Months, freshness and recovery

- Months use UTC, not the viewer's local timezone. `ended_at` determines the month. New months start at zero without deleting history. A match must not precede player registration or be over 30 seconds in the future; future-month results are rejected.
- New events may arrive until month end + 48 hours, exclusively. A closed/deadline-passed month rejects new events; an already accepted receipt can still replay. Close persists a board digest and Top 10. History is not automatically purged.
- SQLite commits the score, per-shard event and receipt together. Redis receives absolute scores with a durable cursor, not unguarded increments. Source lineage, ownership, checksums and offsets are verified.
- A successful normal score request returns 200 with `projected=true`. During projection outage it can return **202 with durable=true/projected=false**: retrying the same match is safe. Ranking reads wait for every shard's authority-head barrier or return 503; they never return partial/stale rankings as complete.
- The authority serializes public writes and multi-phase global reads to provide this barrier. Internal feeds stay available while reads wait. This limits throughput and assumes one active authority process.
- Redis pairs use AOF and asynchronous replication. Failover is **manual and fenced**: stop the old primary, promote its replica, then bring the old node back as a replica. Two reachable masters fail closed. No Sentinel/election is provided. Missing projected data replays from authority; a changed authority lineage requires an explicit rebuild.
- GET `/internal/status` on authority/projectors requires the internal token and exposes source heads, projection position/lineage, poll time/errors. Projection query/feed routes are internal-only. Redis namespaces isolate rebuild/reset; tests only flush wholly owned disposable instances.

The reference supports 10000 registered players and at most 10^9 wins each. Catalog/journal SQLite is not replicated, and no game anti-cheat, full account auth, TLS, online sharding, multi-host HA or automatic history/log cleanup is supplied. No production 25M-user or 2500-score-QPS claim is made. Details: [design](docs/design.md).

## Chapter 26 engineering review — 2026-10-27

The core chapter flows are present: trusted +1 wins, monthly zero-score
membership, Top10, shared competition ranks, exact individual positions and
four neighboring display rows. The review separates journal contracts, RESP,
Lua scripts, candidate validation, profile cache, transport and service runtime.

Ordinary rank queries now share a source read gate; source writes, reconciliation
and rebuilds take exclusive ownership. New-month initialization releases the
read gate before preparing zero-score membership, then captures a fresh shared
snapshot. Eight public operations share sixteen RPC workers; HTTP admits32
requests. All shard phases validate identity, exact source heads and a stable
Redis epoch. A reset between phases fails503, rather than merging generations.
Internal feeds/status bypass public capacity so projection can satisfy barriers.

Names use a process-local1024-entry/1MiB LRU keyed by authoritative
profile_revision and player ID. Renaming invalidates reuse; scores and Top10 are
always queried from Redis. This is not a shared Redis profile cache. Journal
inputs are frozen before writer waits; time is sampled once after acquiring the
writer. Regular wins no longer scan all players. Names are fetched in SQL batches.

Lua prevalidates known key types/index/score transitions before mutation, fences
old poll/reset epochs, and applies absolute scores with position atomically.
Unchanged empty polls validate their epoch without writing AOF. RESP limits total
bytes, nesting and items, and Redis/HTTP calls share deadlines and cancellation.
Accepted work is drained at shutdown; arbitrary SQL/CPU/DNS cannot be forcibly
terminated. Multiple reachable Redis masters still fail closed; promotion remains
an explicitly fenced manual procedure, not consensus.

Validation: **72 tests passed39.110s**, no skips, `-W error`, real Redis8.0.5
under WSL and seven processes. HEAD baseline8 tests passed38.686s in an isolated
snapshot. The added real-Lua test verifies prevalidation, stale rebuild epoch
rejection, replay and no AOF growth during idle polls. The original demo passed
with ranks[1,1,3,3], one duplicate and independent reconciliation,742.600ms.

Measured60 sequential fully projected wins:7.36/s,p50/p95141.863/197.049ms.
Four-client40-query samples:Top10 p50/p95118.700/146.256ms,32.93QPS;
rank+neighbors207.717/240.098ms,18.68QPS. These are small localhost samples,
not controlled speedup or25M-user/2500-QPS evidence. See
[review](docs/chapter26-review.md) and [measurements](benchmarks/results.md).

Authority HA, automatic Redis election, TLS/full accounts, anti-cheat, online
resharding, score-range/DynamoDB alternatives, achieved-time tie breaks and
historical retention remain outside this bounded reference. No commit or push.
