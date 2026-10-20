# Design: Distributed Message Queue

## Roles and safety boundary
A single durable HTTP coordinator owns topic metadata, partition epochs/high water
marks, producer receipts, consumer leases/cursors and transactional visibility.
Three independent brokers own separate disk directories. All user operations go
through the coordinator; brokers accept authenticated internal operations only.
The coordinator serializes publication/election with a lock and SQLite transactions.
This makes epochs and commit metadata authoritative without pretending the reference
implements distributed consensus. Coordinator outage stops reads/writes; restoring
its database is required. It is a deliberate availability bottleneck, not production HA.

## Segments
Each partition generation has append-only numbered segment files. A frame contains
magic, payload length, CRC32 and canonical JSON envelope with offset, timestamp,
producer metadata, text, key, delay, previous hash and SHA-256 chain hash. Index
sidecars record every offset/file position and are rebuilt from verified frames on open.
Log payloads are read on demand. A torn final frame is truncated; complete CRC/hash
failure is corruption and must never be silently discarded. Every accepted append
fsyncs the segment and index. Epoch/base checkpoints and generation pointers use
atomic replace. Process-crash recovery is tested; power-loss/directory fsync
guarantees on every Windows filesystem are not claimed.

## Publication and replication
The coordinator journals one pending batch per partition before network writes.
The journal preserves an exact retry after a lost response. Leader/followers align
to the committed prefix and append the same batch. Behind replicas install a
verified retained prefix into a new generation, then atomically swap their pointer.
Caught-up replicas form the operation's ISR; all members that acknowledged plus a
configured majority are required before high water advances. Unreachable replicas
are excluded from that operation, never below majority. This is quorum-backed
reference replication, not Kafka's full dynamic ISR protocol.

Leader election probes prefix hashes at the coordinator's committed high water.
Only an eligible replica may lead; epoch increments and later sync fences stale
requests. Uncommitted tails are truncated/overwritten before reuse. No broker can
independently publish or acknowledge a user operation, so an isolated old leader
cannot make data visible. A leader-only response means staged/locally persisted,
not visible or quorum durable; background retry commits it once quorum recovers.
The coordinator pending journal is an additional durable copy, but not replicated.
No-ack is a client fire-and-forget adapter with no retry/confirmation contract.

## Consumers and transactions
Membership is lease-based, deterministic round-robin by sorted member ID.
Joining/replacing/expiring a session increments group generation and invalidates
delivery bounds. Commit requires current session, generation, ownership, live
lease and an offset no later than delivered. At-least-once fetch leaves cursor
unchanged; at-most-once persists it before returning. Delayed first records block
later records in the same partition to preserve order.

Transactional processing stages output batches, replicates all of them, then
atomically advances their high waters and the source group cursor in one SQLite
transaction. txid plus request fingerprint deduplicates the operation. After
coordinator crash, unfinished transactional stages are aborted and remain invisible;
an uncommitted broker suffix is repaired on the next write. This guarantees atomic
visibility inside the reference, not arbitrary database/email effects. Retry and
dead-letter helpers use this same boundary; archive replay intentionally allocates
new offsets and is not an exactly-once restore protocol.

## Retention and operations
Default retention is 14 days, optional retained byte cap. Offset floor advances
only through committed records; physical deletion removes complete old segments,
possibly retaining an active segment longer. Chain checkpoint accompanies floor.
Expired consumers receive 410 and explicitly reset; no silent skip. Receipt rows
below floor are removed while producer sequence watermarks prevent old retries
from producing again. Leases, pending replication and cleanup run while active.
Shared client credentials are a trusted-client lab simplification. Production needs
TLS, per-topic ACLs, quotas, consensus metadata, incremental catchup, bounded pending
journals/receipts, observability, backups and sustained workload/chaos validation.
