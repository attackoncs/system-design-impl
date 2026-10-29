# Requirements: Distributed Message Queue (chapter 20)

Source: [System Design Interview.md](<../../System Design Interview.md>), chapter 20,
lines 8020–8996. Core review: 2026-10-26. The executable reference and production
targets must remain distinguishable; not every book-scale deployment is present.

## Core behavior

- Text messages, topics/partitions, independent groups, replay and partition-local
  order. Default retention is 14 days or the local byte cap, whichever comes first.
- Real independent HTTP brokers with separate disks, append-only segmented files,
  CRC/hash chains, offset index, batched fsync and verified crash recovery.
  Complete corruption fails closed; only incomplete active tails may be repaired.
- Fenced producer epochs and per-partition batch sequences, immutable retry input
  and explicit recovery of uncertain writes. Key routing and random unkeyed routing;
  automatic size/linger batching with bounded admission and observable Futures.
- All-ack majority confirmation; leader-only responses remain provisional until
  quorum publication. A bounded one-shot async client adapter does not establish
  a new wire no-ack or process-exit durability guarantee.
- Pull groups with heartbeat, explicit leave, membership rebalance and persisted
  next offsets. Old session/generation/owner commits are fenced. At-least-once and
  at-most-once have distinct cursor commit points.
- Consume/produce transactions atomically publish output high waters and input
  cursor within this metadata/log boundary; external side effects are excluded.
- Scheduled relative/absolute delivery, head-of-line ordering, bounded long polls,
  OR tag filters with scanned next_offset, and increasing partition counts without
  pretending old-key order survives modulo remapping.
- Retry/dead-letter topics and bounded local archive/replay as separate extensions.

## Engineering and validation

Use short metadata transactions, ordered fixed lock stripes, network I/O outside
the SQL writer, per-log locks, complete input ownership, bounded HTTP/RPC and owned
shutdown. Preserve per-partition serialization and multi-output publication while
allowing unrelated partitions to progress. Retention failures require durable
retry and visible maintenance status.

Exercise real independent processes/networks for leader stop, lost responses,
quorum loss/recovery, corrupted disk and rebalance; label controlled in-process
faults separately. Report final evidence in [validation](../benchmarks/results.md)
and chapter gaps in [review](chapter20-review.md); historical measurements retain
their dates. Local timing is not proof of book-scale availability or throughput.

## Explicit extensions

The coordinator remains one SQLite owner with no Raft/etcd or automatic failover.
Dynamic broker placement, partition shrinking, full ISR protocol, incremental
catch-up, zero-copy, cloud tiering, topic ACL/quotas, complete metadata GC, regional
recovery and sustained production validation remain further work.

Only commit when requested by the user. Preserve the existing local-only
requirements/tasks convention; design documents are eligible for an authorized
commit. No commit or push is implied by this review.
