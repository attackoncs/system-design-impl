# Design: Distributed Message Queue

## Roles, modules and concurrency

One durable HTTP coordinator owns topic metadata, partition epochs/high waters,
producer receipts, consumer leases/cursors and transactional visibility. Separate
broker processes own separate disks; user operations go through the coordinator.
This owner is deliberately singular: sharing the metadata database between two
processes does not provide consensus or failover.

The coordinator keeps its public facade while separating replication, groups,
retention, validation and operation-lock modules. Publication/election uses 128
fixed ordered RLock stripes. One partition protocol holds its stripe across
probe/replicate/publish; unrelated stripes can progress. Transactions acquire
source/output and producer/transaction resources in a consistent order. Collisions
serialize unrelated work. A producer identity lock also serializes that producer's
partitions against epoch changes; independent producer IDs can progress on
different partition resources. SQLite WAL still permits only one metadata writer.
Network I/O runs outside metadata transactions; final visibility and membership
checks use short transactions.

Each PartitionLog owns an RLock for reads, appends, synchronization, retention and
generation changes. Broker registry locks protect only loader ownership and
publication. A Future coalesces concurrent opening of one partition; integrity
scans for another partition do not hold a broker-wide disk-I/O lock.

## Segments and recovery

A frame contains magic, bounded payload length, CRC32 and canonical JSON envelope
with offset, timestamp, producer metadata, text/key/tags, due time, previous hash
and SHA-256 hash. Offset/position index sidecars are rebuilt from verified frames.
Bodies are read on demand. A torn active tail is truncated; complete CRC/hash
corruption fails closed. Empty trailing segments do not authorize dropping earlier
corruption.

The complete incoming batch is frozen and validated before truncating provisional
tails or writing data. Adjacent frames targeting one segment are written together;
each touched data and index file is flushed/fsynced once per batch. An I/O failure
poisons the live log object rather than acknowledging through uncertain in-memory
state. Epoch/base checkpoints and generation pointers use atomic replacement.
The implementation attempts directory fsync where supported; universal Windows
power-loss/controller guarantees are outside the verified process-crash boundary.

Frame, batch and snapshot budgets reject oversized work; read budgets may return
shorter pages. This is a bounded local reference: a slow replica may require retention before its retained prefix
fits installation limits. Index metadata remains proportional to retained offsets.

## Publication and replication

The coordinator journals one pending batch per partition before network writes.
Leader/followers align to the committed prefix and append identical canonical
records. Response hashes/offsets and consumer pages are verified against metadata.
Behind replicas install the verified retained prefix in a new generation and
atomically swap their pointer; catch-up is a bounded full snapshot, not incremental
or chunk-streamed replication.

Election probes hashes at the committed high water; only eligible replicas lead.
Epoch increments fence stale requests; malformed synchronization is validated
before truncating a prior provisional suffix. All user-visible publication belongs
to the metadata owner, so an isolated broker cannot independently commit a write.

Default all-ack publication requires the configured broker majority, reporting
actual confirming replicas. This per-operation caught-up set is not Kafka's
dynamic ISR/min.insync.replicas protocol. Quorum loss leaves an uncertain durable
pending batch; retry its exact producer sequence/payload. Leader-only confirmation
returns committed=false and remains invisible until background quorum recovery.
The pending journal is an extra copy on a single unreplicated coordinator disk.

## Producer client

Producer sequences are per epoch/topic/partition. Fixed producer lock stripes
protect one sequence protocol, and messages are copied before retry or queuing.
Uncertain writes retain their batch and reject substitution; retry_pending sends
that same in-memory payload. A new producer epoch fences old clients but does not
deduplicate across process/epoch resets.

send sends a single partition batch. send_many groups by deterministic keyed
routing (random routing for unkeyed records), preserves per-partition input order
and returns input_indexes with receipts. Earlier chunks can succeed before a
later failure; the operation is not a multi-partition transaction.

BufferedProducer owns one worker and a bounded record queue. Batch size/linger
trade off latency and RPC/fsync amortization. Each submit returns a Future;
cancellation before dispatch omits that record, while accepted sends cannot be
rolled back by cancelling a Future. flush/close drain work; callbacks on the worker
must not synchronously join it. Close buffers before Client.

Client fire_and_forget uses one bounded executor, one leader-ack request and no
automatic retry; returned Futures expose errors. Client.close stops admission and
drains accepted synchronous/async calls. No process-exit delivery guarantee or
durable client-side spool is claimed.

## Consumers, filtering and transactions

Members use leases and sorted-ID round-robin assignments. Joining, replacing,
leaving or expiring a session changes generation and invalidates delivery bounds.
Commit checks current session, generation, partition ownership, live lease and
the scanned delivery bound. Membership is rechecked after broker I/O. At-least-once
leaves the cursor unchanged until commit; at-most-once stores it before responding.

Tag filters are OR matches. limit bounds scanned input, not matching output;
next_offset advances across nonmatches, including an empty page. A committed
filter change cannot recover skipped records without reset. Relative delay and
absolute deliver_at are exclusive. A not-yet-due head blocks later records to
preserve partition order; scheduling does not extend retention. Long polling releases operation/SQL locks while waiting;
its wait_seconds bounds waits, not an already accepted RPC.

Processing stages and replicates all output batches, then atomically advances
their high waters and input cursor in one SQLite transaction. txid/fingerprint
deduplicates that request. Crash recovery aborts unfinished transactional stages,
which remain invisible; provisional suffixes are repaired before reuse. Queue
visibility and cursor form the exactly-once boundary; arbitrary external effects,
new txids and explicit reset do not share it.

Increasing a topic's partition count creates empty partitions and bumps all group
generations. Existing logs remain in place. Modulo key routing changes with the
count, so applications must coordinate transition ordering. Shrinking, dynamic
broker membership and automatic old-record redistribution are not implemented.

## Retention and operations

Default retention is 14 days or 8MiB retained envelopes per partition, whichever
comes first. Floor and chain checkpoint advance only across committed records;
physical deletion removes complete old segments. Per-broker cleanup failures have
a durable retry journal. Expired offsets return 410; consumers reset explicitly.
Old receipts can expire without permitting old producer sequences to become new
writes. Epoch/transaction receipt GC is still incomplete.

HTTP admission defaults to 32 handlers, backlog 128 and a 10s absolute socket
deadline, with 32MiB bodies/responses. Auth precedes body reads; framing,
duplicate JSON fields and non-finite values are rejected. Direct RPC uses the same
strict JSON/framing policy, ignores environment proxies, rejects redirects and
keeps normal HTTPS certificate verification. Socket deadlines cannot forcibly
interrupt DNS/SQLite/CPU. Capacity remains reserved until work actually exits.
Shutdown closes request I/O and joins accepted handlers, then drains owned
non-daemon maintenance. Sanitized failures remain visible in maintenance_error.

Local archive/replay is bounded to 100000 records/64MiB and creates new offsets,
not cloud tiering or transactional restoration. Final executable evidence and
measurements are in [validation](../benchmarks/results.md); chapter mapping is in
[review](chapter20-review.md). Production work remains: consensus/failover,
dynamic placement, incremental catch-up, zero-copy, TLS/ACL/quotas, metadata GC,
cloud backup/tiering, regional recovery and sustained workload/chaos validation.
