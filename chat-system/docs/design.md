# Design: Chat System

## Scope and architecture

The local implementation follows Chapter 13's direct/group messaging, device inboxes,
presence, and notification flows. The book separates stateful chat/presence servers
from stateless API services and storage. The local mode composes them in one active asyncio event loop and offloads disk
operations to worker threads. The independent Redis mode below supplies actual
cross-process routing, leases and discovery. [Chapter coverage](chapter13-review.md)
separates tested behavior from deployment scale.

```text
WebSocket clients (one session per user/device)
                |
        JSON WebSocket adapter
                |
          ChatService ------- TokenAuthenticator
          /    |     \
     sessions presence  asynchronous push adapter
          |
     MessageStore protocol
          |
     SQLiteMessageStore
     channels / members / messages / inbox

ServiceDiscovery: independent region/capacity registry for integration
```

## Package structure

```text
chat-system/
  pyproject.toml
  README.md
  README_CN.md
  docs/{requirements,design,tasks}.md
  src/chat_system/
    __init__.py
    models.py       # Config, immutable messages/channels, exceptions
    store.py        # Storage protocol and SQLite adapter
    service.py      # Authentication, sessions, fanout, presence, push integration
    discovery.py    # Reference capacity-aware registry
    transport.py    # Optional JSON/WebSocket adapter
    concurrency.py  # Owned blocking IO and cancellation draining
    distributed.py / redis_backend.py / redis_scripts.py
    push.py / client.py / cluster.py
  examples/
    basic_chat.py
    websocket_server.py
  tests/
    test_chat.py
    test_transport.py
```

## Persistence and message order

- `channels(id, owner, kind)` stores direct/group metadata.
- `members(channel, user)` has a composite primary key.
- `direct_pairs(first_user, second_user, channel)` uniquely indexes a direct pair.
- `messages(id, channel, sender, content, created, client_id)` uses SQLite AUTOINCREMENT.
- `(sender, client_id)` is unique and makes retries idempotent across devices/restarts.
- `inbox(user, message)` holds one reference for every member, including the sender.
- `(channel, id)` and `(user, message)` indexes support ordered history and inbox pagination.

Message insertion and inbox fanout share a transaction. The server commits before
enqueueing live events or calling push. IDs are increasing in this store and are
used for ordering, rather than wall-clock timestamps. A distributed implementation
would replace this with per-channel sequences or a coordinated ID generator and
durable fanout workers. SQLite uses a shared thread-safe connection per store, guarded by a lock, WAL for
file databases, and BEGIN IMMEDIATE for writer admission across instances. The
WebSocket adapter calls async_create_direct/async_create_group/async_add_member,
async_sync/async_history and async_get_message; blocking operations run in worker
threads and finish before cancellation releases their ordering lock. The synchronous
library API remains available. Network reads stream a byte-limited page; low-level
sync/history retain their count-only semantics.

New group members are not backfilled into previous inbox entries. History queries
require current membership and an inbox entry, preventing accidental exposure of
pre-join messages. Direct channel creation reuses an indexed unique pair. Legacy duplicate direct
channels retain their IDs/history; migration chooses one canonical pair for future
creation without merging or deleting old history. Exact get_message lookup requires
the original recipient inbox entry and hides missing/unauthorized IDs alike.

## Sessions, backpressure, and presence

A session binds an authenticated user, device, bounded asyncio queue, and last
heartbeat. A second login for the same user/device replaces the first. Other devices
remain connected. The core checks session identity and credentials on each operation.

Session and presence routing use per-user device indexes and reverse subscriber
indexes. Request validation examines only the relevant user; periodic cleanup does
the global expiry sweep. Token digests are dictionary keys. Presence overflow cascades
use an iterative worklist; expired/revoked subscribers are closed before delivery.

Each new message is enqueued to all recipient devices. Queue overflow closes the
slow session and emits a recovery event. The durable inbox, not the volatile queue,
is the source of truth. Clients reconnect and explicitly sync from their last fully
processed cursor. They must not advance that cursor just because a later live event
arrived: earlier committed messages may not have been delivered live.

The service expires sessions using a monotonic clock. Transport cleanup runs
periodically. Explicit logout closes immediately; unexpected connection loss leaves
the session to expire after the heartbeat grace period. A user remains online while
any device is fresh. Presence subscriptions publish only transitions; authorization
of which contacts can be observed belongs to the embedding application.

## Push and failure semantics

The default push adapter records invocations. Offline recipients trigger one call
per newly committed message. Provider exceptions are recorded and do not change the
send result. Local provider calls share a service-wide semaphore (default 8) and
a finite deadline (10 seconds, including slot wait); failure diagnostics retain the
last 1000 records and a cumulative count. The recording adapter is also bounded.
Durable push retries use the distributed worker described below. A crash after commit and before live/push fanout can lose the notification,
but the message remains retrievable through sync. This is not exactly-once network
delivery; idempotent writes and cursor recovery are the explicit guarantees.

## WebSocket protocol

Use JSON objects with `request_id`, `op`, and operation-specific parameters. First
request: `login` with `token` and `device_id`. Responses use `type=response`, echo
`request_id`, and contain `ok` plus `result` or `error`. Unsolicited events have
`type=message`, `type=presence`, or `type=closed`.

Operations: `login`, `heartbeat`, `direct`, `group`, `add_member`, `send`, `sync`,
`history`, `message` (exact ID with recipient authorization), `subscribe_presence`, `logout`. `send` includes `channel_id`, `content`,
`client_message_id`; `sync` includes `after` and `limit`. Successful sync returns
messages and a `next_cursor`, retaining the input cursor when no messages exist.
WebSocket clients serialize request/response handling separately from event handling.

Tokens are created out of band, never returned through an unauthenticated public
endpoint. The example binds to localhost. Remote deployment requires TLS and an
external authenticator; this demo does not provide account registration or password
storage. Authentication has a timeout and WebSocket frame size is capped while still
allowing a valid Unicode message. Unknown operations and malformed data return safe
errors. Graceful server exit cancels and drains background tasks and closes sessions.
A client acknowledges only a complete last receive page; a temporary file is fsynced
and atomically replaces its cursor. Request deadlines include interleaved events.
Canceled requests discard their connection. Group creation has no retry key, so an
uncertain outcome is reported instead of replayed. See [transport review](transport-review.md).

## Discovery and scale

The in-memory registry chooses a non-full server by region affinity, utilization,
then stable server ID. Callers register updated connection counts; selection does
not reserve capacity. It does not discover failures automatically or route messages
across processes. Shared durable storage, server leases, an inter-server broker,
and global presence are necessary before horizontal deployment.

## Validation

Requirements map to tasks and tests. Core tests use `asyncio.run`, fake clocks
and event barriers, with finite network/provider deadlines in lifecycle tests. WebSocket tests use an ephemeral loopback port and
the optional transport dependency. File-backed restart tests verify durability;
authorization and idempotency tests verify isolation under retries and concurrency.

## Distributed extension

The distributed mode uses independent asyncio WebSocket processes and a shared Redis
backend. A Redis Streams journal replaces local-only fanout; each node independently
consumes and checkpoints its cursor. Redis hashes store channels/messages/retry keys;
sorted sets index user inboxes. A Lua script checks a membership snapshot and commits
the message, all inbox references, the journal event, and push candidates together.
Membership changes trigger snapshot retry rather than partial recipient fanout.

Redis server time is authoritative for node/device leases. A node instance UUID
fences duplicate node IDs; each device session has an independent UUID, so reconnect
on another node invalidates the earlier device session. Atomic admission checks the
node lease and capacity. User presence considers only fresh sessions on nodes with
fresh leases. Nodes poll shared presence for contact subscriptions.

The stateless discovery HTTP API selects live nodes by region and utilization. The
reconnecting client calls it after connection failure, authenticates on a new endpoint,
and replays its durable inbox cursor. Discovery does not migrate live sockets.

Push candidates use a Streams consumer group. Workers reclaim abandoned pending
entries, recheck shared presence, invoke a Redis-recording or HTTP-webhook adapter,
and acknowledge completion. Failure metadata and retry deadlines are durable. Jobs
exhausting retries enter a dead-letter stream. HTTP calls carry an idempotency key;
external providers determine duplicate suppression.

Redis must run with AOF enabled and persistent storage. Scripts use keys in one
hash-tag namespace to preserve atomicity; this reference mode uses a shared primary
and is not a sharded 50-million-user deployment. The optional Sentinel deployment
below supplies Redis primary failover. Cross-region quorum storage remains outside
this reference implementation. Redis outage causes requests to fail rather than accept volatile writes.

Distributed source modules: `redis_backend.py`, `distributed.py`, `push.py`,
`cluster.py`, and `client.py`. CLI: `python -m chat_system.cluster`. Existing local
public APIs stay usable. The transport adapter awaits either local or distributed
service methods, and network tests exercise separate processes through real sockets.

Redis API references: [Lua atomic execution](https://redis.io/docs/latest/develop/programmability/eval-intro/),
[pending stream entry recovery](https://redis.io/docs/latest/commands/xautoclaim/),
and [async client lifecycle](https://redis.readthedocs.io/en/stable/examples/asyncio_examples.html).

## Sentinel and capacity extension

Optional Sentinel configuration replaces the direct Redis URL with an async
`master_for` client. Reads and writes use the discovered primary; promotion causes
the connection pool to refresh its target. Three Sentinel voters (quorum two) and
two Redis replicas accompany the primary. All three data nodes use AOF and persistent
volumes. Sentinel state files are writable and retained. Production voters must
run in independent failure domains; a local compose is a reproducible topology only.

Configured replica acknowledgement uses a nontransactional pipeline containing
the write, a short-lived replication barrier, and `WAIT` on the same connection.
The barrier also confirms existing state when an idempotent retry makes no change.
A failed acknowledgement is an uncertain
result, not a rollback: clients retry the stable key. This narrows asynchronous
replication loss windows but does not make Sentinel a consensus database or guarantee
zero loss under simultaneous failures. Keep at least one healthy replica available
and configure primary write safeguards for partition scenarios.

Per-node capacity indexes are scoped by node instance, with atomic removal from the
previous node on device replacement. Sorted-set counts replace per-login scans.
Expired session records are pruned in bounded batches; ephemeral indexes have TTLs.
Discovery examines live node records and counts their indexes, rather than all devices.
Message fanout looks up only local sessions belonging to channel members. Each
stream batch validates distinct recipients through bounded pipelines and checkpoints
only after every event in that batch is queued. Crash replay can repeat up to 100
live hints; clients deduplicate and recover using the durable inbox. Idle-session
validation also uses bounded pipelines.

The capacity harness drives actual WebSocket endpoints, maintains heartbeats,
collects acknowledgements and delivery IDs, and produces a JSON/Markdown report
with configured/admitted connections, exact successful/missing counts, throughput,
and p50/p95/p99 latencies. Fault testing kills the owned Redis primary and verifies
promotion and continued chat. Capacity reports describe measured local scale only.

References: [Sentinel failover](https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/)
and [WAIT replication acknowledgement](https://redis.io/docs/latest/commands/wait/).


## 2026-10-24 reliability changes

Distributed start/close and device admission are serialized and owned through repeated
caller cancellation. Lease loss marks a node unready and closes local devices. Push
attempts renew a token lease; ACK, retry and dead-letter mutations require that token,
so a reclaimed attempt cannot be overwritten by an obsolete consumer. Already-ACKed
stream entries do not invoke the provider again. HTTP redirects are rejected rather
than treated as successful delivery. Provider side effects still require recipient/
message idempotency; a network timeout cannot prove that no delivery occurred.

Presence probes deduplicate watched users and use bounded Redis pipelines. History
checks recipient visibility in bounded batches instead of per-message round trips.
SQLite IDs are signed integers; Redis sequence allocation stops before 2**53 to keep
sorted-set ordering exact. History and streams have no automatic expiry. Neither the
SQLite adapter nor the single Redis hash-slot deployment claims fifty-million-DAU
capacity. This review did not repeat historical load or Sentinel-primary-crash runs;
current tests and skipped experiments are recorded in [chapter13-review.md](chapter13-review.md).
