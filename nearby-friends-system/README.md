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

Core and reliability unit tests run without a Redis server. Real-network tests
require an actual Redis executable and otherwise explicitly skip; consult the
[dated validation report](benchmarks/results.md) for the executed test count. For Linux, set
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
processes and two WebSocket processes on free localhost ports. Each Redis uses a
fresh random password; cleanup verifies its recorded run ID before shutdown. The
lab stops its owned processes and does not administer an existing Redis service. SQL/log files
remain under ignored `.runtime/lab-<uuid>/` for diagnosis. Default Redis protected
mode remains enabled. No Redis mock is used in network tests.

## Persistent service

Start Redis separately (e.g. localhost 6379 and 6380). Set PYTHONPATH and provision
credentials locally; token files must not exist yet. Provisioning exclusively
creates the token file, flushes/fsyncs it, then inserts the SQL user; a failure
removes only the newly created file whose identity still matches:

```powershell
$env:PYTHONPATH = 'src'
.venv/Scripts/python -m nearby_friends.cli provision --user alice --admin --token-file .runtime/alice.token
.venv/Scripts/python -m nearby_friends.cli provision --user bob --token-file .runtime/bob.token
.venv/Scripts/python -m nearby_friends.cli ring --user alice --nodes examples/brokers.json
.venv/Scripts/python -m nearby_friends.cli serve --port 8090 --cache-url redis://127.0.0.1:6379/0 --max-connections 256 --workers 16 --send-timeout 3
```

In a second terminal, same PYTHONPATH/database/cache/namespace, serve on 8091.
The local SQLite file is deliberately shared by the two processes on the same
host; this is not distributed SQL. Defaults are TTL 600s, reconciliation 1s and
history retention 86400s. TTL/retention persist in database configuration;
changing CLI arguments for an existing database does not override them.

The application admits at most 256 upgraded sessions by default, submits at most
16 synchronous SQL/Redis calls, and allows 3 seconds for one response or a whole
delta batch. These are
configurable with --max-connections, --workers and --send-timeout. The session
limit is not a TCP socket quota: opening handshakes have a 5-second timeout and
backlog 128, and deployment needs transport/load-balancer limits. Server shutdown
stops admission, closes sessions, cancels subscriptions and drains accepted
synchronous work before closing Redis pools. Cancellation cannot interrupt a
running SQLite call or platform DNS resolution, so drain can wait for those.

## WebSocket protocol

Connect with `Authorization: Bearer TOKEN` in upgrade headers. Initial frame is
`{"type":"snapshot","friends":[...],"total":0,"next_offset":null}`.
Send UTF-8 JSON text objects with an optional request_id string (1..64 characters).
Input is capped at 16KiB and outbound application frames at 256KiB; unknown
operation fields, duplicate JSON keys, binary input and non-finite numbers are
rejected. Responses are ack/error;
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
clock skew cannot extend presence. Receipt time and admissibility are measured
after acquiring the SQLite writer, including after a lock wait. Latest accepted
timestamp wins across devices;
there is no device takeover lease. Disconnect retains location until TTL.

Delta changes contain enter/update with friend ID/location/timestamp/distance,
or leave with ID only. Subscriptions cover all friends; authorization and distance
are rechecked on output. Reconnect gets a current snapshot rather than replayed
Pub/Sub. Reconciliation repairs lost hints and applies revocation/expiry within
the configured interval plus processing time. In-flight frames cannot be recalled.
Paging is a live offset view: movement can shift pages; deduplicate by friend ID.
An initial connection snapshots existing valid locations; a new client sends its
first location command to become visible. Explicit snapshot requests return a
page without replacing the live delta baseline. Radius changes still produce
leave/enter/update events; merge pages and deltas by friend ID. Initial page size
20 and subsequent delta frames do not provide an immutable historical cursor.

## Engineering and validation

[Chapter review](docs/chapter18-review.md) maps the book to the implementation.
The domain, immutable routing, schema, protocol and owned worker pool are separate
modules. Reports are read in consistent SQL transactions; output takes a final
SQL snapshot after optional Redis work to recheck TTL, friendship and sharing.
Redis is a bounded mirror: cache hits do not avoid either SQL read.
That final read is the response authorization point; a later revocation can still race
an already prepared/in-flight frame.

Cache repair uses one MGET and pipelines of at most 128 records. Its default
1-second budget is checked between batches; already-running Redis calls may
finish later. A cache read failure does not cause one timeout per friend. Synchronous
Redis clients have 16-connection pools, 0.5-second socket budgets and no automatic
retries. An LRU of at most 32 broker clients leases active publishers so eviction
cannot close a client in use; close waits for accepted operations. Each session's
async subscription client separately owns one connection per broker group.
Redis URLs allow only the db query option, preventing URL overrides of transport
budgets. Redis/cache failures may lose hints; current SQL state remains available.

The [validation report](benchmarks/results.md) contains current test counts,
independent-process evidence and dated measurements. New raw fanout observations
are stored in [review-2026-10-25.json](benchmarks/review-2026-10-25.json).
The original 2026-10-20 experiment used 21 connections and delivered 200/200 updates
with p95 140.323ms; it is a historical local result, not the current measurement
or a production capacity claim.

Current SQL rows are hidden at TTL. An owned background sweep runs at
min(30 seconds, TTL), retries failures and removes expired current/history rows;
individual location updates do not globally sweep. Cleanup can lag due to work
or downtime. Consented history remains bounded by the configured retention;
production disks/backups need protection. Shared SQLite serializes writes, each
session subscribes to all friends and reconciles the complete nearby set. These
costs remain. No claim of 10M connections, 334K updates/s, Cassandra, etcd,
Redis HA, cloud regions, TLS or production SQL HA. Erlang is a design alternative;
random nearby strangers are an optional extension, separate from friend sharing.
See [design](docs/design.md).
