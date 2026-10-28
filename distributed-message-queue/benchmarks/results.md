# Queue verification report

## Core review — 2026-10-26 (Asia/Shanghai)

Python 3.13.9 / Windows 11 build 26100, standard library only. Actual localhost
networking ran in an approved unrestricted process because the Windows sandbox
blocks these socket fixtures. Temporary files were isolated in the workspace;
owned service processes were stopped, with diagnostic disks/logs retained under
ignored `.runtime/lab-<uuid>/`. No external Kafka, Redis, cloud or account was used.

### Final regression

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -q
```

**116 tests passed in 34.735s, no skips and no ResourceWarning.** The pre-change
baseline passed 15 tests in 27.598s. The final suite contains:

| Area | Tests |
| --- | ---: |
| Original log / deterministic state / independent-process cluster | 15 |
| Storage, checkpoint, snapshot, failure and broker partition concurrency | 24 |
| Coordination, fencing, atomic publication, retention and chapter additions | 29 |
| Producer, buffering, async ownership and archive/replay | 19 |
| Real HTTP/RPC framing, deadlines, overload and shutdown | 22 |
| CLI and maintenance lifecycle | 7 |

New regression checks cover frozen inputs, failed fsync and snapshot publication,
committed-prefix rollback rejection, batch writes, independent partition progress,
same-producer fencing, multi-output rollback, retention retry across restart and
partial broker pages. Tags, scheduled heads, leave/expansion, long polling and
at-most-once concurrent fetch are covered with actual SQLite/log state and labeled
in-process transport/clock substitution. Network tests separately exercise real
sockets/processes. One full integration run exposed Windows RST during overload:
the old single receive could leave request bytes unread after sending 503. The
final bounded-drain implementation passes a strict 503 test with a 64KiB body;
the assertion was not weakened to accept arbitrary connection errors.

Client tests verify every key's target, per-partition order, copied retry payloads,
unknown-result recovery, batch size/linger/flush, cancellation before dispatch,
capacity including in-flight work, successful Future preservation before a later
partition failure, accepted-work drain and refusal to join one's own worker.
Archive tests cover captured high water, exclusive creation, incomplete-file
cleanup and complete input validation before replay's first write.

Ruff E9/F, formatting and Python 3.10 syntax passed for all **32 Python files**.
Local Markdown links and `git diff --check` were checked separately.

### Actual independent-process examples

```powershell
python examples/network_demo.py
python examples/chapter_demo.py --output benchmarks/features-2026-10-26.json
```

The original demo still produced offsets [0,1], replayed an identical receipt,
recovered with b1/epoch 1 after b0 stopped, rejected a minority write with 503,
then committed offset 2 after recovery. The queue transaction advanced its input
cursor to 3 and retained exactly one output after an identical txid retry.

The [new chapter feature report](features-2026-10-26.json) records actual HTTP
against three separate broker disks, one coordinator and three TCP proxies:

- Eight buffered messages received quorum-confirmed per-record Futures on both
  keyed partitions; tag filtering returned four checkout messages and advanced
  scan cursors to the respective high waters.
- Explicit leave reassigned partitions [0,1] to the remaining member; expansion
  to three yielded [0,1,2], and the new partition accepted/read back offset 0.
- An absolute scheduled head and its following record were returned by long poll
  after the due time, in order. Retention remains independent of that schedule.

These are synthetic text fixtures, not a production broker deployment. Proxy
processes only control TCP connectivity and contain no queue logic.

### Current small synthetic measurement

```powershell
python tools/benchmark.py --output benchmarks/review-2026-10-26.json
```

The final benchmark ran **after** the final regression/examples, without another
suite or graph rebuild running at the same time. [Raw report](review-2026-10-26.json):

| Metric | Observed |
| --- | ---: |
| Brokers / coordinator / TCP proxies | 3 / 1 / 3 |
| Partitions / producer threads | 3 / 2 |
| Batches × messages / text bytes per message | 12 × 10 / 256 |
| Committed / independently fetched | 120 / 120 |
| Timed production interval | 1.583s |
| Messages per second | 75.79 |
| Batch ACK p50 / p95 | 253.544 / 266.547ms |
| Broker disk bytes, all replicas including indexes/manifests | 215,856 |
| Segment files, all replicas | 54 |
| Request errors | 0 |

Timing includes HTTP/proxies, CRC/hash checks, short metadata transactions,
per-segment batched data/index fsync and majority confirmation. Readback lies
outside the timed production interval. Broker eligibility still scans segments;
this is not incremental replication. Only 12 batch samples inform p95 (sorted
index floor(0.95 × (n−1))). The historical 34.21 messages/s result below had
concurrent experiments, so it is not a controlled before/after speedup ratio.
No sustained throughput, tail-latency SLA or power-loss probability is established.

### Graph evidence and remaining limits

code-review-graph completed a full rebuild (548 files, 6970 nodes, 62814 edges,
no build errors) and a final incremental refresh. Explicit impact analysis on
seven existing core files reported 102 direct changed nodes, 24 impacted nodes,
five additional files, 1933 unresolved call sites and truncated edges. The dynamic
`Coordinator.produce` dispatch caller query returned zero with a missing-edge
warning. The new untracked `producer.py` file was explicitly reported **not
indexed** even after full rebuild. These graph results guide inspection and do
not establish complete call/test coverage; new modules were read and executed
directly. Other chapters' existing uncommitted work also remains in repository
wide graph context. See [chapter mapping](../docs/chapter20-review.md).

One coordinator owns SQLite metadata; the database still has one writer. Ordered
fixed stripes allow unrelated partitions/producers to progress, with collisions
and same-producer operations still serializing. Whole-prefix catchup plus pending
is limited to 24MiB/10000 envelopes (including frame overhead), 2MiB per frame;
larger histories need retention or a future chunked protocol. HTTP body/response
limit is 32MiB. Full indexes, metadata receipts and static broker membership retain
capacity limits. No dynamic ISR/consensus/metadata HA, broker migration, partition
decommission, zero-copy, TLS/ACL, WAN/cloud tiering, sustained soak or power-cut
validation was added. Socket deadlines cannot interrupt accepted DNS/SQL/CPU work.

## Historical verification — 2026-10-20

2026-10-20, Asia/Shanghai. Python 3.13.9, Windows 11 build 26100. Standard
library only. Current restricted Windows sandbox blocks loopback networking;
the successful network runs used approved unrestricted local processes and
workspace-isolated files. No external service, Kafka, Redis or cloud was used.

### Tests

`python -m unittest discover -s tests -v`: **15 passed in 27.619s**.

Five storage tests use actual files: rotation, persisted index rebuilding,
restart, incomplete active header recovery, complete CRC corruption rejection,
epoch fencing, uncommitted suffix truncation, retention checkpoint and full
snapshot installation. This is process/file behavior, not a power-cut test.

Five deterministic state tests replace only broker transport with calls into
real local Broker/file objects. They use an injected clock/failure set to verify
lease expiry and reassignment, delayed head-of-line order, retention/expired
receipts, failed transactional output invisibility/atomic retry, and pending
journal recovery after coordinator reopen. These are explicitly unit fault
injections, not distributed network evidence.

Five independent-process network tests start three brokers with separate roots,
a coordinator and three transparent TCP proxies (seven processes per fixture):

- Exact producer retry reuses the response; changed payload conflicts. Kill b0,
  observe a different leader with higher epoch and intact committed records.
  Restart b0, produce more, restart coordinator and verify persisted replay/fetch.
  Opening a new producer epoch fences the old one.
- Cut both follower proxies while their real backend health endpoints still
  respond. Majority write returns 503; fetch exposes no uncommitted tail.
  Leader-only response is provisional. Restore one follower and retry the same
  batch: one committed offset, no duplicate.
- At-least-once repeated fetch, undelivered commit rejection, real join/rebalance,
  old-generation commit rejection, at-most-once pre-response cursor advancement
  and same-member session replacement fencing.
- Transaction replay yields one output, input cursor moves atomically; retry
  topic delay, attempt-limit dead letter, local archive and replay are exercised.
- Corrupt a stopped leader's log byte, restart it and verify it is excluded from
  eligibility while an intact follower serves the acknowledged message.

The TCP partition is a controlled transport fault on localhost, not an actual
cross-datacenter network. Proxies contain no replication/queue implementation.
Corrupt replicas fail closed and need operator rebuild; automatic disk repair
is not claimed. Slow disk, metadata disk corruption, coordinator failover and
power loss were not tested.

### Demo

`python examples/network_demo.py` completed with 3 broker roots, 1 coordinator
and 3 TCP proxy processes. First offsets [0,1]; exact retry identical. After b0
termination the leader was b1, epoch 1. With another broker partitioned, write
returned **503**. Restoring connectivity committed offset **2**. The input
transaction cursor became **3**, and its replay still left exactly **1** output.
All owned processes were stopped; diagnostic directories remain ignored.

### Small synthetic measurement

`python tools/benchmark.py --output benchmarks/local-results.json`

| Metric | Observed |
| --- | ---: |
| Brokers / coordinator / TCP proxies | 3 / 1 / 3 |
| Partitions / producer client threads | 3 / 2 |
| Batches × messages / text bytes per message | 12 × 10 / 256 |
| Submitted / committed and read back | 120 / 120 |
| Timed production interval | 3.508s |
| Messages per second | 34.21 |
| Batch-confirmation p50 / p95 | 551.999 / 619.402ms |
| Broker disk bytes (all replicas, including indexes/manifests) | 215,769 |
| Segment files across all replicas | 54 |
| Request errors | 0 |

Producer timing includes HTTP, TCP proxies, coordinator serialization, CRC/hash
checks, SQLite commit and actual record/index fsync on brokers. Broker status
revalidates segments for integrity before election/replication; this is deliberately
expensive in the reference. Readback time is outside the timed production interval.
The example and suite were also running during part of this experiment, so host
contention can affect latency. p95 is sorted index floor(0.95×(n−1)), only 12 batch
samples. No production throughput, tail-latency SLA or durability probability is
established. Raw evidence: [local-results.json](local-results.json).

### Boundaries

No Raft/etcd, metadata quorum/HA, coordinator election, TLS, per-topic identity,
cloud/WAN, incremental replica catchup, sustained soak or disaster recovery was
used. Coordinator must be restored from its durable metadata for service to
continue. Exactly-once applies only to queue-visible output high waters plus
input cursor in one metadata transaction. External side effects, new producer
epochs, explicit cursor resets and archive replay are outside that boundary.
No-ack client API has no completion guarantee and is not a measured reliable mode.
Default 14-day retention is additionally capped at 8MiB per partition; active
segments can survive logical expiry. Receipt/epoch metadata needs future GC and
pending workloads need quotas. Full catchup RPC is capped at 32MiB; very large
retained catalogs need chunked/incremental replication. Windows directory/pointer
power-failure durability is not established by these tests.
