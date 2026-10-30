# Scale From Zero to Millions of Users — Chapter 2

<!-- LEARNING_GUIDE_START -->
## Code reading guide

[Learn through a concrete example: architecture, state changes and annotated source (Chinese)](docs/learning-guide_cn.md).
<!-- LEARNING_GUIDE_END -->

A staged, standard-library experiment showing what changes when a single application becomes a stateless web tier with a cache, read replicas, sharded data and durable background work. **All services run on one host using real loopback HTTP. This is not a million-user capacity or multi-region HA demonstration.**

| Stage | Processes | Components |
|---|---:|---|
| `single` | 1 | HTTP app, SQLite profiles/shared-session records, local background-consumer thread |
| `balanced` | 8 | Round-robin load balancer, 2 stateless web nodes, 1 primary, 1 read replica, cache, worker, local edge-cache model |
| `sharded` | 10 | Same web/cache/worker/edge tiers, with 2 primary/replica pairs |

The edge cache is an explicit local **CDN model**, not a real CDN or GeoDNS service. Data and session records use independent primary database files per shard. Read replicas copy profile changes only. They do not replace primaries on their own. An attached `Fence` can promote a caught-up replica for profile writes after the primary lease expires; sessions and jobs stay on the original primary. The default 1/8/10-process stages do not attach that fence.

## Run

Python 3.10+, no third-party runtime dependencies. From this directory in PowerShell:

```powershell
$env:PYTHONPATH = 'src'
New-Item -ItemType Directory -Force .runtime/tmp | Out-Null
$env:TEMP = (Resolve-Path .runtime/tmp).Path
$env:TMP = $env:TEMP
python -m pytest tests -q
python examples/network_demo.py
python examples/network_demo.py --stage sharded
python tools/benchmark.py --requests 30 --clients 4
```

The suite has 73 tests, including the original multi-process HTTP cases. The demo starts each topology with fresh isolated data, logs in two synthetic users, updates their profiles, verifies replica/cache reads and drains background jobs. Labs stop only their own processes and remove temporary databases on exit. Each stage is a separate experiment, **not an online migration procedure**.

For a persistent topology:

```powershell
python tools/init_config.py --stage sharded
```

Inspect `services` in `.runtime/private-config.json`. Start each listed node in its own terminal with `PYTHONPATH=src` and the same working directory:

```powershell
python -m scale_from_zero.cli primary0 --config .runtime/private-config.json
python -m scale_from_zero.cli replica0 --config .runtime/private-config.json
python -m scale_from_zero.cli primary1 --config .runtime/private-config.json
python -m scale_from_zero.cli replica1 --config .runtime/private-config.json
python -m scale_from_zero.cli cache --config .runtime/private-config.json
python -m scale_from_zero.cli web0 --config .runtime/private-config.json
python -m scale_from_zero.cli web1 --config .runtime/private-config.json
python -m scale_from_zero.cli lb --config .runtime/private-config.json
python -m scale_from_zero.cli worker --config .runtime/private-config.json
python -m scale_from_zero.cli edge --config .runtime/private-config.json
```

For `balanced`, omit primary1/replica1; `single` has only the `single` node. Use a fresh config/database directory for a different stage. The generator refuses overwrite, and existing data nodes reject changed shard counts/indices. Credentials are Git-ignored, but the generator does not change filesystem permissions. Only loopback endpoints are accepted.

## HTTP experiment

Use the config `entry` URL, and choose a generated username/password from `users`:

1. `POST /v1/login` with `{"user":"alice_0","password":"<generated lab credential>"}` returns a one-hour bearer session.
2. Send that session as `Authorization: Bearer <session>` to `POST /v1/profile`:

```json
{"request_id":"write1","expected_version":0,"display_name":"Synthetic User","city":"Test City"}
```

3. `GET /v1/profile` returns the owned profile, version, read source and web node. Successive requests can use different web nodes with the same session.
4. `GET /v1/profile?consistency=strong` bypasses cache/replica and reads the primary. `?min_version=1` accepts a cache/replica value only at or beyond that version, otherwise falls back to primary. A requested future version returns409.

Profile strings are bounded to128 characters. Updates require optimistic `expected_version` and a stable per-user request ID. Same key/content replays its original result; changed content or stale expected version returns409. A retry of an older successful request returns that older result, not the latest profile. It is safe to reuse the original key after a lost write response. Sessions are stored on the user's primary (hashed bearer values), not in a web process. The configuration login scheme is a synthetic fixture, not production password/KYC management.

The load balancer retries a failed upstream for GETs and profile POSTs carrying an idempotency key. Login POST is not automatically retried. It uses round-robin attempts and request-time failure fallback, not a background health/election service.

## Consistency and background work

Default reads are eventual: cache-aside tries a bounded32-entry LRU cache with2-second TTL, then the corresponding asynchronous read replica, then primary on replica failure/missing/too-old version. Cache failure is bypassed. Writes commit before best-effort cache invalidation. Generation tickets reject pre-invalidation fills, and observed write-version floors reject older values. Failed invalidation, cache restart or eviction of bounded floor metadata can still permit replica staleness: **TTL bounds one entry's lifetime, not total replica staleness**. Use strong/min-version reads when freshness matters. Authentication itself still depends on the primary session store.

Each profile write atomically persists the profile/version, idempotency result, replication change and outbox job in one SQLite transaction. Replicas enforce sequence/checksum/deduplication and apply a change/cursor together. Sessions, idempotency indices and jobs are not replicated. The default replica loop does not promote itself. Promotion is an explicit `POST /internal/promote` on a replica that has a `Fence` attached.

The worker consumes per-primary durable jobs with five-second leases (direct Store.claim retains a one-second default) and claim-token fencing. Its independent inbox deduplicates stable event IDs. A lost ACK causes repeated attempts but one recorded event effect; an unavailable shard does not starve another. Events stand in for background notification/audit work, and no email or external message is sent. Delivery is at least once, with unbounded retry in this small lab, no production broker/DLQ or exactly-once network guarantee.

## Assets and diagnostics

`GET /assets/app-v1.txt` and `app-v2.txt` work through the origin entry URL or the edge URL. Their versioned paths avoid replacing an object under the same immutable identity. Edge caches have30-second TTL. When edge is unavailable, clients can explicitly use origin; no DNS or browser fallback is installed.

Use the admin token for `GET /admin/status` on a specific service. Available metrics include web-node identity, load-balancer fallback count, primary profile reads, replica cursor, cache hits/misses/evictions, pending jobs and consumer unique/attempt counts. Worker `POST /admin/control` accepts `{"lose_ack":true}` for post-consume ACK-loss experiments; reset false to recover. These are instructional counters, not production monitoring/alerts.

## Limits and evidence

See [design](docs/design.md), [verification/measurements](benchmarks/REPORT.md) and [current sample](benchmarks/review-2026-10-27.json) and [historical sample](benchmarks/latest.json). Adding tiers creates network cost at small scale; the benchmark measures that tradeoff and primary profile-read reduction without assuming a speedup.

Shard routing is fixed SHA256(user) modulo count. There is no online resharding. Primaries, balancer, session stores, worker and cache/edge instances remain individual services; primary failure stops authentication and strong reads for that shard unless a `Fence` has promoted profile writes. Replicas support profile-read fallback without a fence. No cross-host durability, automatic database failover of the default stages, real CDN, real GeoDNS service, geographic deployment, million-user load, production identity/TLS or autoscaling has been verified. Each primary retains at most1000 profile changes and1000 live sessions; complete history is kept for replay. Related later chapters provide deeper standalone components, not automatically integrated infrastructure here.

## Engineering review (2026-10-23)

Routing/replay policy lives in `balancer.py`; owned background operations and shutdown live in `workers.py`. Cache synchronization is internal, so direct reuse is thread-safe. Network I/O and cache copying do not hold routing/cache metadata locks. See [per-project review](../DEEP_REVIEW_PROGRESS.md) for findings, regression evidence and remaining limits.

## 2026-10-27 deep review

The complete suite has66 tests, including five actual network experiments. Install optional tests with `pip install -e ".[test]"`; unittest discovery alone misses pytest regressions. Validation disables unrelated plugin autoload, without skipping project cases.

`examples/resilience_demo.py` runs nine processes/three Web nodes. `tools/init_config.py --web-nodes 3` preconfigures1–8 Web nodes. Admin `GET/POST /admin/backends` accepts `{ "nodes": ["web0","web1"] }` using configured names only. In-flight requests retain their candidate snapshot; future requests use the new pool. Restart restores the configured pool; this is not service discovery/autoscaling. Edge `POST /admin/invalidate` accepts `{ "asset": "app-v1.txt" }`. Client `assets.fetch_asset(config, asset)` explicitly falls back to origin on edge failure, without replaying404.

Cache capacity is32 entries/512 KiB. Fill tickets expire on invalidation/restart, and up to1000 observed-version floors prevent stale repopulation. Eventual consistency remains after failed invalidation, floor eviction or restart. Replication binds the durable source database identity, checks cursor/prefix, complete batch and per-user versions, then commits the batch atomically. Legacy replicas first verify retained prefixes; empty-disk primary replacement and mixed-version rolling upgrades are unsupported.

Claim replies include actual lease/attempt/source identity. ACK validates live expiry, token, original payload and source; consumer IDs are source-scoped. Legacy unscoped inbox rows bind on first matching delivery and cannot prove earlier source identity: back up/audit before upgrade. Profile replicas still exclude sessions, deduplication and jobs and cannot be promoted directly.

HTTP is32 connections/service and15-second I/O budget,64 KiB input/8 MiB output. Bounded RPCs share request/poll deadlines, reject ambiguous paths/queries/JSON/credentials/framing and bypass proxies/redirects. Non-daemon workers stop/close/join; socket cancellation cannot force-stop SQL/CPU/custom adapters. Contracts, profiles, sessions, jobs, replication, consumer and protocol concerns have focused modules with compatible Store/Consumer/partition exports.

See [chapter mapping and open architecture](docs/chapter02-review.md). Production database HA, a real GeoDNS service, regional disk sync, autoscaling, migration and production observability remain open. Measurements include regressions; no overall speedup is claimed.

## Datacenter routing and cache policy (2026-10-29)

`regions.GeoDNS` maps a client location to the nearest of two to four data-center entries. If that center is unhealthy, resolution walks to the next healthy center, so a two-center outage sends every lookup to the survivor. Unknown locations use a stable hash. This directory does not speak DNS and does not copy disks. Cross-center profile copy is still `workers.replicate_once` pointed at the home primary.

`leadership.Fence` is a shared SQLite lease. The writer heartbeats a cursor. `POST /internal/promote` on a replica takes the next epoch only after that lease expires and only when the replica cursor is at least the published cursor. A lagging replica is refused. After promotion, profile updates run on the replica and the old primary's writes fail closed. Session create, claim, and ack stay on the original primary role.

`Cache` remains LRU by default and also accepts `policy="lfu"` or `"fifo"`. `Cache.set(key, text, ttl)` / `Cache.get(key)` store the chapter's short text values. `CacheCluster` reads the first healthy member and writes every member, so one failed cache is not a single point of failure. These types are not wired into the default one-cache process topology.
