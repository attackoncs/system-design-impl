# Nearby Friends verification

## Core review — 2026-10-25 (Asia/Shanghai)

Windows 11 build 26100 / Python 3.13.9, websockets 16.1.1, redis-py 6.4.0,
real Redis 8.0.5 under WSL Ubuntu-26.04. The existing review virtual environment
provided Python dependencies; no other chapter's application code is imported.
An existing unpacked local Redis binary/library directory supplied the executable,
which is not packaged in this module. The lab launched its own Redis processes,
with independent random passwords and recorded run IDs, and stopped only owned
services. Logs/SQLite files stay under ignored `.runtime/lab-<uuid>/` for diagnosis.

### Final regression

With NEARBY_WSL_DISTRO, NEARBY_WSL_REDIS_BINARY and NEARBY_WSL_REDIS_LIB pointing
to the installed executable/libraries, run from the project directory:

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -q
```

**76 tests passed in 21.360s, no skips**: 9 original core tests, 18 Store/Ring
regressions, 17 Backend regressions, 23 protocol/session/work tests, 4 provisioning
tests and 5 actual Redis integration tests (three original multi-process scenarios
and two new real Lua/cache checks). The full run used an approved unrestricted
local process because of Windows sandbox socket restrictions, with isolated
workspace temporary paths. Asyncio's slow-task diagnostics reflect synchronous
lab process startup/cleanup, not failed assertions. No ResourceWarning appeared.

Coverage added in this review:

- Real SQLite writer contention: timestamp admissibility and receipt TTL after
  waiting, ordered concurrent devices, privacy-vs-write races, history rollback,
  concurrent initialization, persisted settings and index migration.
- Cache read failure does not trigger a timeout per friend; malformed/short data,
  bounded pipelines and repair budgets preserve the SQL result. Delayed Redis
  work cannot bypass final friendship, sharing, TTL or viewer movement checks.
- Broker-client creation is serialized, clients are bounded and leased during
  use, idle reconfiguration reclaims them, close drains accepted work.
- Strict JSON/fields, request correlation, failed initialization cleanup,
  unchanged subscription retention, slow consumers, paginated snapshots that
  preserve leave/update events, and one deadline across a multi-frame delta batch.
- A bounded executor retains a slot until underlying I/O exits even when its
  awaiter is cancelled. Actual WebSocket clients verify admission, controlled
  stop and cancellation draining; those tests use a controlled backend, while
  the five Redis integration tests use real Redis and the original three also
  run two separate WebSocket processes against shared local metadata.
- Real Lua rejects delayed older revisions, preserves receipt-based remaining
  TTL, and repairs malformed JSON, boolean revisions and wrong-type Redis keys.
- Existing credential files survive; disk/database provisioning failures remove
  only the newly created matching file and do not leave an unusable account.

Synthetic Redis errors, controlled backend delays and deliberately skipped
Pub/Sub publication remain labeled fault injection. They are distinct from real
network/process outage tests. Ruff E9/F, formatting, Python 3.10 syntax and local
Markdown links were checked separately. The [chapter review](../docs/chapter18-review.md)
maps requirements, graph findings and remaining extensions.

### Example and final warm fanout

`python examples/network_demo.py` completed with two Redis / two WebSocket
processes. Dateline distance **222.39016046812768m**; first cross-node entry
**458.127ms**, publish succeeded and authorized snapshot total became **0** after
sharing was disabled. Initialization/subscription time is included in this
single observation.

```powershell
python tools/benchmark.py --connections 20 --updates 10 --output benchmarks/review-2026-10-25.json
```

| Metric | Observed |
| --- | ---: |
| Independent WebSocket / Redis processes | 2 / 2 |
| Observer / total connections | 20 / 21 |
| Publisher updates | 10 |
| Expected / observed deliveries | 200 / 200 |
| End-to-end p50 | 74.340ms |
| End-to-end p95 | 164.934ms |
| Maximum observed | 197.023ms |

[Raw current report](review-2026-10-25.json). Warm connections; timing starts
before publishing and ends when each observer receives the corresponding
revision. p95 is sorted index floor(0.95×(n−1)). It includes SQL, Redis,
WebSocket transport and client scheduling. The benchmark ran after the final
suite, not concurrently with it. The older measurement below used a different
host state and implementation; these small samples do not establish a stable
speedup, sustainable capacity, WAN latency or production availability.

### Operational limits

The default 256 admission limit counts upgraded application sessions; it is not
an absolute TCP/handshake quota. Up to 16 synchronous operations are submitted;
waiting callers remain within admitted sessions and owned maintenance tasks.
One response or whole delta batch shares the configured send timeout (default
3s). Redis pool/socket bounds and between-batch repair budgets cannot interrupt
DNS, SQLite, CPU work or an already-running call; shutdown waits for accepted
operations. A final SQL read authorizes output; revocation after that read can
still race prepared/in-flight frames.

Shared SQLite, two full SQL reads per nonempty snapshot, per-session friend
subscriptions and full reconciliation remain scaling costs. Redis is a bounded
mirror and cache hits do not avoid SQL. Physical user/history/cache sharding,
Redis primary/standby, consensus discovery, multi-region, TLS/production identity,
load-balancer draining and the book's 10M connections/334K reports per second
are not deployed or validated. Nearby strangers and Erlang are optional/separate
routes. File-first provisioning handles ordinary failures but is not a cross-
filesystem/SQL atomic transaction or power-loss recovery protocol.

## Historical verification — 2026-10-20

The original observations below retain their date and implementation state.

Date: 2026-10-20, Asia/Shanghai. Windows 11 build 26100 / Python 3.13.9,
websockets 16.1.1, redis-py 6.4.0; real Redis 8.0.5 under WSL Ubuntu-26.04.
The existing chat-system virtual environment supplied installed client libraries;
no chat code is imported. Redis came from an existing unpacked local lab binary,
with LD_LIBRARY_PATH pointing to its unpacked library directory. That binary is
not packaged with this module. Tests ran outside the restricted Windows network
sandbox using isolated workspace temporary directories.

## Correctness and failure paths

Command: `python -m unittest discover -s tests -v` with real Redis executable
environment configured. **12 tests passed in 16.160s** (9 core tests and 3
real-network tests). Coverage:

- Hashed authentication, mutual friendship consent, unknown requests and revocation.
- Opt-in sharing, privacy disable/re-enable requiring a new report, self-only history,
  retention and erasure, strict ordering across devices and reopen.
- TTL based on receipt time, client skew validation, dateline, pole and 5mi boundary.
- Unit-injected corrupt/unavailable cache with authoritative fallback and ACL recheck.
- Consistent-hash stability, limited remapping on addition, admin and epoch CAS checks.
- Two independent WebSocket nodes, real Redis channels and TTL keys, cross-node
  entry/update/exit, reconnect snapshot, actual broker removal/addition and failure.
- A deliberate skipped publication (fault injection) followed by snapshot-based
  reconciliation; not a claim of lossless Pub/Sub. Inactivity tests shorten TTL to
  3 seconds; ring operational tests use 60 seconds to avoid unrelated expiration.

No Redis or WebSocket mocks are used in the three network tests. Cache corruption
and loss of one publication are explicitly synthetic fault injections. All Redis
processes are disposable and owned by the test; WS processes share a SQLite file
for local metadata, so this does not prove multi-host database behavior.

## Executable example

`python examples/network_demo.py` completed with 2 Redis / 2 WebSocket processes.
Dateline friend distance: **222.39016047087586m**. Cross-node first entry:
**438.436ms** including subscription initialization; publish succeeded; after
sharing disabled the authorized snapshot contained **0** friends.

## Warm fanout experiment

`python tools/benchmark.py --connections 20 --updates 10 --output benchmarks/fanout.json`

| Metric | Observed |
| --- | ---: |
| Independent WS nodes / Redis processes | 2 / 2 |
| Observer / total connections | 20 / 21 |
| Publisher updates | 10 |
| Expected / observed deliveries | 200 / 200 |
| End-to-end p50 | 82.252ms |
| End-to-end p95 | 140.323ms |
| Maximum observed | 171.749ms |

Timing starts before the publisher command and ends when each observer receives
the matching revision. It includes SQL, Redis, WebSocket transport and client
scheduling. Connections are warm. p95 uses sorted index floor(0.95×(n−1)); no
throughput extrapolation, WAN claims or sustainable-capacity conclusion is drawn.
The demo/load experiment ran while the separate final test suite was finishing,
so host contention can affect timings. Raw data: [fanout.json](fanout.json).

## Unimplemented production capabilities

No 10M concurrent connections or 334K updates/s validation, distributed SQL,
Cassandra history shards, consensus discovery, Redis HA/Sentinel, cloud regions,
production identity, TLS proxy or sustained soak was used. Pub/Sub remains lossy;
periodic reconciliation is intentional. Shared SQLite and per-connection broker
subscriptions are reference implementations with scaling costs. Logical TTL
filtering is immediate at expiry; physical cleanup runs opportunistically while
service is active and does not certify any legal data-retention compliance.
