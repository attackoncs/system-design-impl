# Chat System

A Python reference implementation of Chapter 13, **Design A Chat System**, from
*System Design Interview*. The core uses only the standard library; an optional
WebSocket adapter provides actual network communication.

## Features

- Authenticated user/device sessions with replacement and token revocation.
- Direct conversations and owner-managed groups of up to 100 members.
- Text messages of fewer than 100,000 characters with increasing message IDs.
- SQLite-backed history and atomic per-user inbox fanout, including sender devices.
- Idempotent sends using a client-provided message ID, persistent across restarts.
- Cursor pagination and independent multi-device synchronization.
- Bounded live queues with explicit reconnect/sync recovery for slow consumers.
- Heartbeat presence, multiple-device aggregation, and presence subscriptions.
- Pluggable offline push integration and region/capacity-aware reference discovery.
- Redis-backed independent chat nodes, durable stream fanout, shared device presence,
  fenced sessions, leased discovery, reconnect recovery, and retrying push workers.

中文文档：[README_CN.md](./README_CN.md)。Architecture and guarantees:
[docs/design.md](./docs/design.md).

## Installation and tests

Requires Python 3.9+.

```bash
cd chat-system
pip install -e .           # Standard-library-only core
pip install -e ".[dev]"  # pytest and optional WebSocket support
pytest -q
python examples/basic_chat.py
```

## Core example

```python
import asyncio
from chat_system import ChatService, SQLiteMessageStore

async def main():
    store = SQLiteMessageStore("chat.sqlite3")
    chat = ChatService(store)
    try:
        alice = chat.connect(chat.auth.issue("alice"), "phone")
        channel = chat.create_direct(alice, "bob")
        message = await chat.send(alice, channel.channel_id, "Hello", "alice-request-1")
        bob = chat.connect(chat.auth.issue("bob"), "laptop")
        print(chat.sync(bob, after=0))
        print(message.message_id)
    finally:
        store.close()

asyncio.run(main())
```

The default store is in-memory. Pass a file path to persist messages and membership.
Close the store when finished. The token authenticator is in-memory and intentionally
separate from chat persistence; reprovision credentials after restart or replace it
with an application's authentication provider.

## WebSocket demo

```bash
pip install -e ".[websocket]"
python examples/websocket_server.py
```

The demo binds to `ws://127.0.0.1:8765`, writes to `chat-demo.sqlite3`, and prints
locally provisioned tokens for Alice, Bob, and Carol. Set `CHAT_DB` to change the
database path. Connect with any WebSocket client and use text JSON frames:

```json
{"request_id":1,"op":"login","token":"TOKEN_FROM_SERVER","device_id":"phone"}
{"request_id":2,"op":"direct","recipient_id":"bob"}
{"request_id":3,"op":"send","channel_id":"CHANNEL_FROM_RESPONSE","content":"Hello","client_message_id":"unique-request-1"}
{"request_id":4,"op":"sync","after":0,"limit":100}
{"request_id":5,"op":"heartbeat"}
```

Send requests sequentially or correlate by `request_id`. A response has
`type: "response"`, `ok`, and `result` or `error`. Live events use `message`,
`presence`, or `closed` as their type. Other operations are `group` (`members`),
`add_member` (`channel_id`, `user_id`), `history` (`channel_id`, `after`, `limit`),
`subscribe_presence` (`user_ids`), and `logout`.

Send an application heartbeat every five seconds. Default expiry is 30 seconds;
protocol-level WebSocket pings do not replace the application heartbeat. Unexpected
transport loss retains online status until expiry; explicit logout closes immediately.

Each device must save its own last fully processed sync cursor. After reconnect,
request pages with that cursor and process them before saving `next_cursor`. Live
events can be deduplicated by message ID, but must not advance the sync cursor
past messages that haven't been processed. A `closed` event instructs reconnect/sync.
Network sync/history responses also cap payload bytes, so large Unicode messages
may result in fewer messages than requested. Continue using the returned cursor.

## Boundaries and extension points

- Local mode uses one process/event loop with synchronous SQLite operations.
  Distributed mode supplies shared persistence, stream routing, and leases through
  Redis. Neither mode demonstrates 50-million-user capacity.
- `MessageStore` is replaceable in local mode; distributed mode uses `RedisBackend`.
- `RecordingNotifier` records offline push calls; real provider delivery and durable
  push retries are not included. Failures appear in `notification_failures`.
- Commit happens before live delivery. A crash between commit and fanout is recovered
  through sync; live events and push notifications are not guaranteed exactly once.
- `ServiceDiscovery` is an independent in-memory selection example, not automatic
  failover or cross-server routing. Caller-supplied counts are not capacity reservations.
- Tokens are provisioned out of band. Remote deployment needs TLS and external
  authentication; presence visibility should be authorized by a contact policy.
- New group members cannot retrieve pre-join history. Attachments, encryption,
  search, and read receipts are outside this chapter's initial implementation scope.

## Distributed deployment

Install `pip install -e ".[distributed]"`. Provide a reachable Redis 6.2+ primary
with AOF enabled, persistent storage, and `maxmemory-policy noeviction`. The supplied
container composition runs Redis, two independent chat nodes, discovery, and a push
worker. From this directory:

```bash
docker compose up --build -d
docker compose run --rm node-a token alice
docker compose run --rm node-a token bob
```

Those commands provision persistent credentials and print each token locally.
Run the reconnecting client on the host with `CHAT_TOKEN` set to one of them:

```powershell
$env:CHAT_TOKEN = 'TOKEN_FROM_PROVISIONING'
$env:CHAT_DEVICE_ID = 'phone'
python examples/reconnecting_client.py
```

The default discovery endpoint is `http://127.0.0.1:8080/discover`. Directly querying
it returns the selected node's endpoint, capacity, and active connection count.
The two host WebSocket endpoints are ports 8765 and 8766. Use the JSON protocol
above to publish messages. Send on one node while the recipient connects to the
other; the Redis journal routes the event across processes.

Without containers, start Redis yourself and run these commands in separate terminals:

```bash
python -m chat_system.cluster token alice
python -m chat_system.cluster token bob
python -m chat_system.cluster node --node-id node-a --port 8765 --public-url ws://127.0.0.1:8765 --region east
python -m chat_system.cluster node --node-id node-b --port 8766 --public-url ws://127.0.0.1:8766 --region west
python -m chat_system.cluster discovery --port 8080
python -m chat_system.cluster worker
```

Set `CHAT_REDIS_URL` or pass `--redis-url` **before** the subcommand to change the
broker endpoint. Use a common `CHAT_NAMESPACE` across cooperating processes.
Optional `--lease`, `--poll`, `--heartbeat-timeout`, and `--capacity` on node commands
configure failure detection and admission. Clients heartbeat every five seconds;
default node lease is ten seconds and device timeout is thirty seconds.

The reconnecting client saves an acknowledged inbox cursor in `chat-cursor.txt`.
If a chat process dies, its node lease expires, discovery chooses another live node,
and the client reauthenticates there and retrieves unprocessed messages. Session
UUIDs fence older connections; an old device cannot overwrite the replacement's
heartbeat, close it, or submit a late message. Redis server time governs leases.

### Durable push workers

By default, the worker stores deduplicated delivery records in Redis. Set
`CHAT_PUSH_URL` to a provider bridge endpoint and optionally `CHAT_PUSH_TOKEN` to
use actual HTTP POST delivery. The body contains `user_id` and `message`;
`Idempotency-Key` is stable for a recipient/message. APNs, FCM, or email integrations
can live behind that bridge and require independently supplied credentials.

Workers use a consumer group, reclaim abandoned jobs, and persist exponential retry
deadlines. Five failed attempts move a job into the dead-letter stream. Configure
`--max-attempts`, `--retry-base`, and `--reclaim-ms` as needed. External delivery is
at least once: the provider must deduplicate by the idempotency key. Shared presence
is checked at processing time; a job for an online user is acknowledged without push.

### Guarantees and deployment limits

- Lua atomically commits messages, membership-snapshot fanout, the broker event,
  and push candidates. Concurrent cross-node retries share the same deduplication key.
- Each node independently reads the durable stream and checkpoints after local
  enqueueing. Replays can duplicate live events; clients recover/deduplicate through sync.
- All Redis keys use one hash-tag namespace. This preserves atomic operations but
  does not provide storage sharding or unlimited throughput. Streams and chat history
  are retained; production operators need a retention/archive policy.
- Chat-node failure recovery is implemented. Redis-primary automatic failover,
  replication/quorum durability, and geographically replicated storage are not included
  in the supplied composition. Use an appropriately managed Redis endpoint for those needs.
- A Redis outage fails requests with a controlled unavailable response; messages are
  not acknowledged using volatile fallback storage.
- The compose endpoints bind to localhost. Public deployment needs TLS, authenticated
  Redis access, and externally reachable advertised node URLs.

### Real distributed tests

The tests launch actual independent OS chat processes and kill one to verify
discovery failover and cursor recovery. Configure an isolated Redis test endpoint:

```powershell
$env:CHAT_TEST_REDIS_URL = 'redis://127.0.0.1:6379/0'
pytest -q
```

Without that variable the real-Redis tests are explicitly skipped. Each test uses
and deletes only its own random namespace; it never flushes a Redis database.
Container execution is separate from the directly exercised process tests.

## Design references

The repository's local Chapter 13 is the feature source. The optional transport
uses the [websockets asyncio server API](https://websockets.readthedocs.io/en/stable/reference/asyncio/server.html).
