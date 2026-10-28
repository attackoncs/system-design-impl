# Distributed Message Queue (chapter 20)

Partitioned, retained text logs with independent HTTP brokers, integrity checks,
producer fencing/replay, consumer groups and atomic consume/produce visibility.
[中文](README_CN.md). The 2026-10-26 core review adds automatic batching, explicit
uncertain-write recovery, group leave/partition growth, tag filters, scheduled
delivery and long polling. The coordinator is one durable SQLite process;
consensus metadata and production availability remain outside this reference.

## Run

Python 3.10+, standard library only. From this directory:

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -v
python examples/network_demo.py
python examples/chapter_demo.py --output benchmarks/features-2026-10-26.json
python tools/benchmark.py --output .runtime/benchmark.json
```

Real-network fixtures start three independent brokers with separate disks, one
coordinator and three transparent TCP fault proxies. Proxies relay/cut TCP and
contain no queue logic. Owned processes stop on exit; private config, disks and
logs remain under ignored `.runtime/lab-<uuid>/`. Kafka/Redis/cloud accounts are
not required. Final tests and measured performance are recorded in
[validation](benchmarks/results.md), with chapter coverage in the
[review record](docs/chapter20-review.md).

For persistent operation, generate private credentials once. In four terminals,
set `$env:PYTHONPATH = 'src'` and run one role per terminal:

```powershell
python tools/init_config.py
python -m message_queue.cli broker --root .runtime/b0 --config .runtime/secrets.json --port 9711
python -m message_queue.cli broker --root .runtime/b1 --config .runtime/secrets.json --port 9712
python -m message_queue.cli broker --root .runtime/b2 --config .runtime/secrets.json --port 9713
python -m message_queue.cli coordinator --root .runtime/coordinator --config .runtime/secrets.json --port 9710
```

Each process exclusively owns its root; never share broker disks or run multiple
coordinators against the same database. Keep Windows paths/topic names short.
The default bind is localhost. Client token authorizes all coordinator operations;
cluster token authorizes broker RPC. Remote deployment requires TLS, service
identities and topic ACLs. Optional install: `python -m pip install -e .`.

## Python producer

With servers running and PYTHONPATH set:

```python
import json
from pathlib import Path
from message_queue.client import Client

config = json.loads(Path('.runtime/secrets.json').read_text())
with Client('http://127.0.0.1:9710', config['client_token']) as queue:
    queue.call('/topic', topic='events', partitions=3)
    producer = queue.producer('application-a')
    receipt = producer.send(
        'events', [{'key': 'customer-1', 'value': 'hello', 'tags': ['paid']}]
    )
    with producer.buffered('events', batch_size=20, linger_seconds=0.02) as batch:
        future = batch.submit({'key': 'customer-1', 'value': 'next'})
        batch.flush()
        acknowledgement = future.result()
    page = queue.call(
        '/fetch', topic='events', partition=receipt['partition'],
        tags=['paid'], wait_seconds=1
    )
```

`send` sends one partition batch. With no explicit partition, keyed records must
map to one partition; use `send_many` to group mixed keys. Unkeyed messages use a
random partition in the Python helper. Low-level `/produce` without an explicit
partition rejects batches whose keys span partitions; entirely keyless batches
use a deterministic producer fallback. Supply partition
when exact retry routing must be controlled. `send_many` accepts at most 1000 records, splits into batches
of 1..100 and returns receipts with `input_indexes`; earlier batches can succeed
before a later failure, so it is not one transaction.

A producer uses per-partition sequence numbers and fixed lock stripes rather
than one lock across all network I/O. Messages are copied before queuing/retry.
After transport/503 uncertainty, a different batch cannot replace the unresolved
sequence. Resend the same payload or use `retry_pending(topic, partition)`.
Opening the same producer ID again increases its epoch and fences older clients;
deduplication does not span new epochs. Existing in-memory sequence/pending state
does not survive a client process crash.

`buffered` owns one non-daemon worker. Defaults: batch size 100, linger 10ms,
1000 outstanding records; maximum capacity 10000 and linger 5s. `submit` returns
a Future; full admission raises BufferError. `flush/close` drain accepted work
and Future.result exposes individual failures. Close buffered producers before
closing their Client. A callback running on the batching worker cannot
synchronously flush/close that worker.

`Client.fire_and_forget(request)` performs one leader-ack call without automatic
retry. It uses a bounded executor (default 64 outstanding calls, four workers),
returns a Future, and rejects excess work. `Client.close` drains accepted async
and synchronous calls. A completed Future only confirms the selected leader-ack
contract; process termination can still lose unconfirmed work.

## RPC and delivery semantics

Every route is POST JSON with `Authorization: Bearer TOKEN`. Coordinator routes
use the client token; broker routes are private cluster operations.

| Route | Parameters / behavior |
| --- | --- |
| /topic | topic, partitions=1, retention_seconds=1209600, max_bytes=8388608, segment_bytes=1048576 |
| /expand | topic, partitions greater than current count, at most 64 |
| /producer | producer ID; opens/fences an epoch |
| /produce | topic, producer, epoch, sequence, messages, optional partition/acks |
| /fetch | topic, partition, offset=0, limit=100, optional tags/wait_seconds |
| /join | topic, group, member; returns session/generation/assignments |
| /leave | topic, group, member, session; explicit removal/rebalance |
| /heartbeat | topic, group, member, session; renew lease/current assignments |
| /group-fetch | identity/partition, optional limit/semantics/tags/wait_seconds |
| /commit | identity/partition plus next_offset |
| /reset | identity/partition plus offset=earliest/latest/integer |
| /process | txid, source identity/partition, expected_offset, next_offset, outputs |
| /retain | topic; enforce time/byte floor and retry physical cleanup |
| /metadata, /health | current state, metadata_ha=false, maintenance_error when CLI-managed |

Text is at most 16KiB UTF-8; a wire producer batch has 1..100 records. Optional
`key` is a string up to 1000 characters. Up to 16 unique identifier `tags` may be
attached. Fetch `tags` is a nonempty OR filter. `limit` bounds scanned records:
even an empty filtered page may advance `next_offset` over nonmatching records.
Advance using that returned cursor; changing a group's filter after committing
cannot recover skipped records without an explicit reset.

`delay_seconds` (0..86400) and absolute Unix `deliver_at` are mutually exclusive.
A delayed head blocks later records, including filtered reads, to preserve
partition order. Scheduling does not extend retention, so a record can expire
before becoming due. `wait_seconds` is 0..5; long polling releases partition/SQL locks
between attempts. This bounds the wait phase, not an already accepted broker RPC.

Partition expansion creates empty partitions and changes group generations;
members must refresh assignments. It does not move existing records. CRC32
modulo routing changes when the count changes, so old and new records of one key
may occupy different partitions. Coordinate producers if that transition requires
ordering. Broker membership is static; partition shrinking is not supported.

`acks='all'` requires the configured broker majority and reports actual replica
confirmations. The per-operation caught-up set is not Kafka's dynamic ISR
protocol. Quorum loss returns 503 with a possibly pending outcome: retry the
identical producer sequence/payload. Pending tails are never consumer-visible.

`acks='leader'` returns committed=false after leader persistence and durable
single-coordinator staging. Background retries can later obtain quorum and
publish. It is neither immediately readable nor a quorum-durable confirmation;
the pending journal is not itself replicated.

Groups default to 10s leases. Join/replacement/leave/expiry changes generation,
invalidates delivery bounds and fences stale commits. At-least-once fetch keeps
the cursor until explicit commit; at-most-once advances before the response and
may lose work. Commit cannot skip beyond the scanned delivery bound. Offsets
mean the next record, not the last processed record.

`/process` replicates output stages, then atomically commits output high waters
and the source cursor in one metadata transaction. A txid/fingerprint retry reuses
the result. This exactly-once boundary excludes arbitrary external databases,
email, HTTP side effects, new txids and explicit cursor resets. Unfinished
transaction stages abort after coordinator restart; provisional broker tails
are repaired before reuse.

## Persistence, concurrency and limits

Log batches validate their complete input before writing, coalesce segment writes
and fsync each touched data/index file once per batch. Per-log locks protect
append/read/retention; broker loading is coalesced without a global disk-I/O lock.
Complete CRC/hash corruption fails closed; only torn active tails are truncated.
Fsync and atomic replacement do not establish universal Windows power-loss safety.

Coordinator protocols use 128 ordered lock stripes, short WAL metadata
transactions and network I/O outside the SQL writer. Different partitions can
progress unless their stripes collide or they share a producer/resource lock.
The server serializes one producer ID across partitions to fence epoch changes;
use distinct producer IDs for independent partition writers. SQLite still has one metadata writer.
Transactions hold their source/output resources in a consistent order.
Retention failures are durably retried and visible as maintenance status.

HTTP defaults: 32 active handlers, 128 listen backlog, 10s absolute I/O budget and
32MiB request/response bounds. CLI options `--concurrency` (1..256),
`--request-timeout` (0..3600, exclusive zero), and `--maintenance-interval`
(default 1s) adjust local limits. Auth precedes body reads; duplicate framing/JSON
fields, non-finite numbers and unsupported encodings fail. Direct RPC ignores
environment proxies, rejects redirects, checks bounded response framing and
supports normal HTTPS certificate validation. Socket deadlines cannot forcibly
stop DNS, SQLite or application CPU work; their slots remain occupied until real
exit. Shutdown drains accepted handlers and owned maintenance instead of
abandoning daemon threads. Worker failures log sanitized messages and retry.

Default retention is 14 days or 8MiB retained envelopes per partition, whichever
comes first. Cleanup removes complete old segments; an active segment may remain.
Expired offsets return 410 and need reset. Expired receipts do not allow reusing
old producer sequences. Producer epochs and transaction receipts lack full GC.

Local frame, batch, read and catch-up budgets make this a bounded reference, not
an unlimited stream. A slow replica beyond the snapshot budget cannot be caught
up automatically; apply retention or extend replication. `Client.archive` writes
a new local JSON file, bounded to 100000 records/64MiB; delay or racing retention
may abort it. `replay` validates then sends batches with new target offsets and
new delivery times. Neither is cloud tiering or exactly-once restoration.

Final evidence is in [validation](benchmarks/results.md), with
[design](docs/design.md) explaining the boundaries. Historical 2026-10-20 results
are retained under their original date. Missing production extensions include
consensus/coordinator failover, dynamic broker placement, incremental catch-up,
zero-copy transport, quotas/ACLs, cloud tiering, cross-region recovery and sustained
load/chaos validation.
