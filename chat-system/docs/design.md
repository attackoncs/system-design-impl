# Design: Chat System

## Scope and architecture

The local implementation follows Chapter 13's direct/group messaging, device inboxes,
presence, and notification flows. The book separates stateful chat/presence servers
from stateless API services and storage. Here they run in one asyncio event loop;
the interfaces and reference registry illustrate those boundaries without pretending
to deploy a distributed cluster.

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
- `messages(id, channel, sender, content, created, client_id)` uses SQLite AUTOINCREMENT.
- `(sender, client_id)` is unique and makes retries idempotent across devices/restarts.
- `inbox(user, message)` holds one reference for every member, including the sender.
- `(channel, id)` and `(user, message)` indexes support ordered history and inbox pagination.

Message insertion and inbox fanout share a transaction. The server commits before
enqueueing live events or calling push. IDs are increasing in this store and are
used for ordering, rather than wall-clock timestamps. A distributed implementation
would replace this with per-channel sequences or a coordinated ID generator and
durable fanout workers. SQLite operations are synchronous and intentionally small;
a production storage adapter must avoid blocking the event loop.

New group members are not backfilled into previous inbox entries. History queries
require current membership and an inbox entry, preventing accidental exposure of
pre-join messages. Direct channel creation reuses the existing pair.

## Sessions, backpressure, and presence

A session binds an authenticated user, device, bounded asyncio queue, and last
heartbeat. A second login for the same user/device replaces the first. Other devices
remain connected. The core checks session identity and credentials on each operation.

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
send result. Push retries and a durable notification outbox are future integration
work. A crash after commit and before live/push fanout can lose the notification,
but the message remains retrievable through sync. This is not exactly-once network
delivery; idempotent writes and cursor recovery are the explicit guarantees.

## WebSocket protocol

Use JSON objects with `request_id`, `op`, and operation-specific parameters. First
request: `login` with `token` and `device_id`. Responses use `type=response`, echo
`request_id`, and contain `ok` plus `result` or `error`. Unsolicited events have
`type=message`, `type=presence`, or `type=closed`.

Operations: `login`, `heartbeat`, `direct`, `group`, `add_member`, `send`, `sync`,
`history`, `subscribe_presence`, `logout`. `send` includes `channel_id`, `content`,
`client_message_id`; `sync` includes `after` and `limit`. Successful sync returns
messages and a `next_cursor`, retaining the input cursor when no messages exist.
WebSocket clients serialize request/response handling separately from event handling.

Tokens are created out of band, never returned through an unauthenticated public
endpoint. The example binds to localhost. Remote deployment requires TLS and an
external authenticator; this demo does not provide account registration or password
storage. Authentication has a timeout and WebSocket frame size is capped while still
allowing a valid Unicode message. Unknown operations and malformed data return safe
errors. Graceful server exit cancels background tasks and closes sessions.

## Discovery and scale

The in-memory registry chooses a non-full server by region affinity, utilization,
then stable server ID. Callers register updated connection counts; selection does
not reserve capacity. It does not discover failures automatically or route messages
across processes. Shared durable storage, server leases, an inter-server broker,
and global presence are necessary before horizontal deployment.

## Validation

Requirements map to tasks and tests. Core tests use `asyncio.run` and fake clocks
without sleep-based assertions. WebSocket tests use an ephemeral loopback port and
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
and is not a sharded 50-million-user deployment. Redis primary/replica failover and
cross-region quorum storage are separate operational work, not implemented by node
lease failover. Redis outage causes requests to fail rather than accept volatile writes.

Distributed source modules: `redis_backend.py`, `distributed.py`, `push.py`,
`cluster.py`, and `client.py`. CLI: `python -m chat_system.cluster`. Existing local
public APIs stay usable. The transport adapter awaits either local or distributed
service methods, and network tests exercise separate processes through real sockets.

Redis API references: [Lua atomic execution](https://redis.io/docs/latest/develop/programmability/eval-intro/),
[pending stream entry recovery](https://redis.io/docs/latest/commands/xautoclaim/),
and [async client lifecycle](https://redis.readthedocs.io/en/stable/examples/asyncio_examples.html).
