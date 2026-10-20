# Nearby Friends (chapter 18)

Live authorized nearby friends over WebSockets, Redis TTL cache and per-user
Pub/Sub channels. Includes mutual friendship consent, opt-in sharing, ordered
multi-device reports, configurable 5mi radius, inactivity expiry, history consent,
consistent-hash broker routing and snapshot recovery. [中文](README_CN.md).

## Run

Python 3.10+, `websockets` 14–16 and `redis` 5–6. From this directory:

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -e .
.venv/Scripts/python -m unittest discover -s tests -v
```

Nine core tests run without a Redis server. Three real-network tests require an
actual Redis executable and otherwise explicitly skip. For Linux, set
`NEARBY_REDIS_BINARY=/usr/bin/redis-server`. For Windows with Redis in WSL:

```powershell
$env:NEARBY_WSL_DISTRO = 'Ubuntu-26.04'
$env:NEARBY_WSL_REDIS_BINARY = '/usr/bin/redis-server'
.venv/Scripts/python -m unittest discover -s tests -v
.venv/Scripts/python examples/network_demo.py
.venv/Scripts/python tools/benchmark.py --connections 20 --updates 10 --output .runtime/fanout.json
```

Use the actual installed executable path. Optional NEARBY_WSL_REDIS_LIB supplies
LD_LIBRARY_PATH for an unpacked Redis build. The lab starts two isolated Redis
processes and two WebSocket processes on free localhost ports. It never connects
to an existing production Redis and stops owned processes on exit. SQL/log files
remain under ignored `.runtime/lab-<uuid>/` for diagnosis. Default Redis protected
mode remains enabled. No Redis mock is used in network tests.

## Persistent service

Start Redis separately (e.g. localhost 6379 and 6380). Set PYTHONPATH and provision
credentials locally; token files must not exist yet:

```powershell
$env:PYTHONPATH = 'src'
.venv/Scripts/python -m nearby_friends.cli provision --user alice --admin --token-file .runtime/alice.token
.venv/Scripts/python -m nearby_friends.cli provision --user bob --token-file .runtime/bob.token
.venv/Scripts/python -m nearby_friends.cli ring --user alice --nodes examples/brokers.json
.venv/Scripts/python -m nearby_friends.cli serve --port 8090 --cache-url redis://127.0.0.1:6379/0
```

In a second terminal, same PYTHONPATH/database/cache/namespace, serve on 8091.
The local SQLite file is deliberately shared by the two processes on the same
host; this is not distributed SQL. Defaults are TTL 600s, reconciliation 1s and
history retention 86400s. TTL/retention persist in database configuration;
changing CLI arguments for an existing database does not override them.

## WebSocket protocol

Connect with `Authorization: Bearer TOKEN` in upgrade headers. Initial frame is
`{"type":"snapshot","friends":[...],"total":0,"next_offset":null}`.
Send JSON objects with an optional string request_id. Responses are ack/error;
independent delta messages may arrive between responses.

| op | Fields and behavior |
| --- | --- |
| sharing | enabled boolean, default false |
| friend.request | target user ID |
| friend.accept | target requesting user; requires incoming request |
| friend.remove | target; revokes both directions |
| location | latitude, longitude, timestamp (Unix seconds), optional device ID |
| snapshot | optional radius_m (default 8046.72, max 100000), offset, limit (default 20, max 100) |
| history.consent | enabled; disabled by default, disabling erases retained history |
| history | offset, limit; always self-only, regardless of other supplied fields |
| ring | admin only: nodes map and expected_epoch CAS |

Both users must opt into sharing and mutually accept friendship. Suggested report
interval is 30s. Reports must be newer than the user's previous timestamp, no older
than TTL, and at most 30s in the future. Server receipt controls TTL, so client
clock skew cannot extend presence. Latest accepted timestamp wins across devices;
there is no device takeover lease. Disconnect retains location until TTL.

Delta changes contain enter/update with friend ID/location/timestamp/distance,
or leave with ID only. Subscriptions cover all friends; authorization and distance
are rechecked on output. Reconnect gets a current snapshot rather than replayed
Pub/Sub. Reconciliation repairs lost hints and applies revocation/expiry within
the configured interval plus processing time. In-flight frames cannot be recalled.
Paging is a live offset view: movement can shift pages; deduplicate by friend ID.

## Verified scope

[Results](benchmarks/results.md): 12 tests including three actual multi-process
network tests; 21 connections and 200/200 fanout deliveries, p95 140.323ms in a
local experiment. History/cache corruption unit faults are explicitly injected;
transport integration uses real Redis and WebSockets. No claim of 10M connections,
334K updates/s, Cassandra, etcd, geographic deployment, TLS or production SQL HA.
See [design](docs/design.md). Query coordinates/tokens are not access-logged.
Current SQL rows expire logically at TTL and sweep within up to 30s thereafter
while the server runs; Redis copies expire by TTL. Consented history is bounded
by retention and requires protected database backups/disks in production.
