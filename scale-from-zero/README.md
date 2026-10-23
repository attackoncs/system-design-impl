# Scale From Zero to Millions of Users — Chapter 2

A staged, standard-library experiment showing what changes when a single application becomes a stateless web tier with a cache, read replicas, sharded data and durable background work. **All services run on one host using real loopback HTTP. This is not a million-user capacity or multi-region HA demonstration.**

| Stage | Processes | Components |
|---|---:|---|
| `single` | 1 | HTTP app, SQLite profiles/shared-session records, local background-consumer thread |
| `balanced` | 8 | Round-robin load balancer, 2 stateless web nodes, 1 primary, 1 read replica, cache, worker, local edge-cache model |
| `sharded` | 10 | Same web/cache/worker/edge tiers, with 2 primary/replica pairs |

The edge cache is an explicit local **CDN model**, not a real CDN or GeoDNS. Data and session records use independent primary database files per shard. Read replicas copy profile changes only; they are not promotable replacements for primaries.

## Run

Python 3.10+, no third-party runtime dependencies. From this directory in PowerShell:

```powershell
$env:PYTHONPATH = 'src'
New-Item -ItemType Directory -Force .runtime/tmp | Out-Null
$env:TEMP = (Resolve-Path .runtime/tmp).Path
$env:TMP = $env:TEMP
python -m unittest discover -s tests -v
python examples/network_demo.py
python examples/network_demo.py --stage sharded
python tools/benchmark.py --requests 30 --clients 4
```

Eleven tests cover components plus four independent-process HTTP cases. The demo starts each topology with fresh isolated data, logs in two synthetic users, updates their profiles, verifies replica/cache reads and drains background jobs. Labs stop only their own processes and remove temporary databases on exit. Each stage is a separate experiment, **not an online migration procedure**.

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

Default reads are eventual: cache-aside tries a bounded32-entry LRU cache with2-second TTL, then the corresponding asynchronous read replica, then primary on replica failure/missing/too-old version. Cache failure is bypassed. Writes commit before best-effort cache invalidation. A concurrent stale fill can race invalidation, and replica lag can cause old values to be cached again: **TTL bounds one entry's lifetime, not total replica staleness**. Use strong/min-version reads when freshness matters. Authentication itself still depends on the primary session store.

Each profile write atomically persists the profile/version, idempotency result, replication change and outbox job in one SQLite transaction. Replicas enforce sequence/checksum/deduplication and apply a change/cursor together. Sessions, idempotency indices and jobs are not replicated; no automatic primary promotion is attempted.

The worker consumes per-primary durable jobs with one-second leases and claim-token fencing. Its independent inbox deduplicates stable event IDs. A lost ACK causes repeated attempts but one recorded event effect; an unavailable shard does not starve another. Events stand in for background notification/audit work, and no email or external message is sent. Delivery is at least once, with unbounded retry in this small lab, no production broker/DLQ or exactly-once network guarantee.

## Assets and diagnostics

`GET /assets/app-v1.txt` and `app-v2.txt` work through the origin entry URL or the edge URL. Their versioned paths avoid replacing an object under the same immutable identity. Edge caches have30-second TTL. When edge is unavailable, clients can explicitly use origin; no DNS or browser fallback is installed.

Use the admin token for `GET /admin/status` on a specific service. Available metrics include web-node identity, load-balancer fallback count, primary profile reads, replica cursor, cache hits/misses/evictions, pending jobs and consumer unique/attempt counts. Worker `POST /admin/control` accepts `{"lose_ack":true}` for post-consume ACK-loss experiments; reset false to recover. These are instructional counters, not production monitoring/alerts.

## Limits and evidence

See [design](docs/design.md), [verification/measurements](benchmarks/REPORT.md) and [raw sample](benchmarks/latest.json). Adding tiers creates network cost at small scale; the benchmark measures that tradeoff and primary profile-read reduction without assuming a speedup.

Shard routing is fixed SHA256(user) modulo count. There is no online resharding. Primaries, balancer, session stores, worker and cache/edge instances remain individual services; primary failure stops authentication/writes/strong reads for that shard. Replicas support profile-read fallback only. No cross-host durability, automatic database failover, real CDN, GeoDNS, geographic deployment, million-user load, production identity/TLS or autoscaling has been verified. Each primary retains at most1000 profile changes and1000 live sessions; complete history is kept for replay. Related later chapters provide deeper standalone components, not automatically integrated infrastructure here.

## Engineering review (2026-10-23)

Routing/replay policy lives in `balancer.py`; owned background operations and shutdown live in `workers.py`. Cache synchronization is internal, so direct reuse is thread-safe. Network I/O and cache copying do not hold routing/cache metadata locks. See [per-project review](../DEEP_REVIEW_PROGRESS.md) for findings, regression evidence and remaining limits.
