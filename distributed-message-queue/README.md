# Distributed Message Queue (chapter 20)

Partitioned, retained text logs with independent HTTP brokers, CRC/hash chains,
producer fencing/replay, consumer groups and atomic consume/produce visibility.
[中文](README_CN.md). This reference uses a single durable coordinator, not a
consensus metadata cluster or production HA system.

## Run

Python 3.10+, standard library only. From this directory:

```powershell
python -m unittest discover -s tests -v
python examples/network_demo.py
python tools/benchmark.py --output .runtime/benchmark.json
```

Each real-network fixture starts three independent brokers with separate disks,
one coordinator and three transparent TCP fault proxies. The proxies only relay
or cut TCP; they contain no queue/replication logic. All listen on localhost and
are stopped on exit. Private config, disks and logs remain under ignored
`.runtime/lab-<uuid>/`. No external accounts or Redis/Kafka are needed.

For persistent operation, create private credentials once:

```powershell
python tools/init_config.py
$env:PYTHONPATH = 'src'
python -m message_queue.cli broker --root .runtime/b0 --config .runtime/secrets.json --port 9711
```

Run the following in three other terminals, setting PYTHONPATH in each:

```powershell
python -m message_queue.cli broker --root .runtime/b1 --config .runtime/secrets.json --port 9712
python -m message_queue.cli broker --root .runtime/b2 --config .runtime/secrets.json --port 9713
python -m message_queue.cli coordinator --root .runtime/coordinator --config .runtime/secrets.json --port 9710
```

Each process must exclusively own its root directory; do not share broker disks or
start two coordinators against one metadata database. Keep Windows roots/topic
names short enough for local path limits. Protect secret files and disks; remote
deployment needs TLS, per-topic ACLs and service identities. Client token permits
all coordinator operations in this trusted-client lab. Cluster token permits
internal broker operations. Optional editable install: `python -m pip install -e .`.

## Python client

With servers running and PYTHONPATH set:

```python
import json
from pathlib import Path
from message_queue.client import Client

config = json.loads(Path('.runtime/secrets.json').read_text())
queue = Client('http://127.0.0.1:9710', config['client_token'])
queue.call('/topic', topic='events', partitions=3)
producer = queue.producer('application-a')
receipt = producer.send('events', [{'key': 'customer-1', 'value': 'hello'}])
messages = queue.call('/fetch', topic='events', partition=receipt['partition'])
```

Producer helper retries the same sequence/payload after transport/503 errors.
Sequences are per producer epoch/topic/partition and identify an entire batch.
Calling `/producer` again increments the epoch and fences the old instance;
duplicates across new epochs are not suppressed. Persist epoch/sequence and use
the low-level API when recovery requires retaining an existing producer identity.

## Protocol and semantics

All routes are POST JSON with `Authorization: Bearer TOKEN`. Public means a
coordinator endpoint protected by the client token; broker endpoints are internal.

| Route | Parameters / behavior |
| --- | --- |
| /topic | topic, partitions=1, retention_seconds=1209600, max_bytes=8388608, segment_bytes=1048576 |
| /producer | producer ID; opens/fences an epoch |
| /produce | topic, producer, epoch, sequence, messages, optional partition and acks |
| /fetch | topic, partition, offset=0, limit=100; committed records only |
| /join | topic, group, member; returns session, generation, assignments |
| /heartbeat | topic, group, member, session; refreshes lease/current assignments |
| /group-fetch | identity/fencing fields plus partition, optional limit/semantics |
| /commit | identity/fencing fields plus partition and next_offset |
| /reset | identity/fencing fields plus partition and offset earliest/latest/integer |
| /process | txid, source identity/partition, expected_offset, next_offset, outputs |
| /retain | topic; enforce time/byte floor and whole-segment cleanup |
| /metadata, /health | topic/partition state; coordinator HA is explicitly false |

Text is limited to 16KiB UTF-8, batches to 100 records. Key routing uses CRC32
modulo a fixed partition count; no global order and no online partition expansion.
Messages may specify delay_seconds (0..86400); a delayed head blocks later records
to retain partition ordering. TTL/retention is independent of delivery.

`acks='all'` is the default majority-confirmed commit. The response lists the
replicas that actually persisted it. Unavailable peers are excluded, never below a
configured majority. This is an operation's caught-up replica set, not Kafka's
full dynamic ISR protocol. Quorum loss returns 503 with uncertain/pending outcome;
retry the identical producer sequence and payload. No user can read a pending tail.

`acks='leader'` returns committed=false after local leader persistence and durable
single-coordinator staging. Background retry later replicates and commits it;
this is not a quorum-durable or immediately readable confirmation. Coordinator's
pending journal is an extra durable copy, but is not replicated. The low-level
`Client.fire_and_forget(request)` issues one daemon-thread leader-ack request and
never retries or reports success; exiting the client may drop it. No-ack delivery
has no completion guarantee and is not the recommended baseline.

Groups default to 10s leases; heartbeat before expiry. Membership replacement,
expiry or changes increment generation, invalidate delivery bounds and fence old
commits. At-least-once fetch leaves the durable cursor until explicit commit;
at-most-once advances before the HTTP response and can lose work. Commits cannot
skip undelivered records. Offset means **next** record, not last processed record.

`/process` stages and replicates all output batches, then atomically commits output
high waters and input cursor. txid/payload retries reuse the same response. This
exactly-once boundary covers only queue visibility plus cursor; it excludes email,
arbitrary databases, external HTTP calls and operations using different txids or
explicit cursor resets. Unfinished transaction stages are aborted after coordinator
restart. Broker tails are then repaired on later writes.

## Retention and extensions

Default retention is 14 days **or 8MiB retained envelopes per partition, whichever
is reached first**. The byte cap is deliberately small for this reference. Physical
cleanup removes complete old segments; an active segment may remain longer.
Expired offsets return 410 and require explicit reset. Replay receipts below the
floor expire; sequence watermarks prevent reusing an old sequence as a new write.
Transaction receipts and producer epoch watermarks are not garbage-collected yet.

`Client.retry` atomically commits an input cursor while writing a delayed retry
topic or dead-letter topic after its attempt limit. `Client.archive` exclusively
writes a local retained-prefix JSON file; delayed records must first become due.
`Client.replay` creates new offsets in a destination topic. Archive is not verified
cloud tiering, and replay is not exactly-once restoration.

[Validation](benchmarks/results.md): 15 tests, including five real multi-process
fault/network tests. 120/120 messages were committed/read back in a small synthetic
run at 34.21 messages/s. Integrity-first full segment checks and per-record fsync
are expensive; this is no production throughput claim. See [design](docs/design.md).
Coordinator outage stops service; there is no Raft/etcd, coordinator HA, WAN/cloud
deployment, TLS proxy, cross-region durability or sustained production validation.
