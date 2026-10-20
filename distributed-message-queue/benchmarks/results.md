# Queue verification report

2026-10-20, Asia/Shanghai. Python 3.13.9, Windows 11 build 26100. Standard
library only. Current restricted Windows sandbox blocks loopback networking;
the successful network runs used approved unrestricted local processes and
workspace-isolated files. No external service, Kafka, Redis or cloud was used.

## Tests

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

## Demo

`python examples/network_demo.py` completed with 3 broker roots, 1 coordinator
and 3 TCP proxy processes. First offsets [0,1]; exact retry identical. After b0
termination the leader was b1, epoch 1. With another broker partitioned, write
returned **503**. Restoring connectivity committed offset **2**. The input
transaction cursor became **3**, and its replay still left exactly **1** output.
All owned processes were stopped; diagnostic directories remain ignored.

## Small synthetic measurement

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

## Boundaries

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
