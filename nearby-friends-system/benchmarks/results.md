# Nearby Friends verification

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
