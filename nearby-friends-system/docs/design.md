# Design: Nearby Friends

## Responsibilities and state

The reference uses independent WebSocket processes sharing one local SQLite WAL
file, plus real Redis cache/Pub/Sub processes. It implements the chapter's Redis
route; Erlang/BEAM is an alternative, and nearby random strangers are extra credit.
Neither is required for the authorized-friends API. Production distribution is
separate from this single-host persistence model.

| Module | Responsibility |
| --- | --- |
| core.py | Transactional users, mutual friendships, settings, ordered reports and history |
| models.py | Domain errors, canonical JSON, IDs, location/page/duration validation and stable spherical distance |
| schema.py | Idempotent tables and expiry/reverse-friendship index migration |
| routing.py | Validated Redis URLs and immutable SHA-256 consistent-hash rings |
| backend.py | Optional Redis mirror/repair, best-effort hints, final authorized snapshots and client ownership |
| protocol.py | Bounded UTF-8 JSON parsing, duplicate/non-finite rejection and operation field contracts |
| work.py | Bounded synchronous executor and cancellation-aware draining |
| server.py | Session admission, serialized delivery, subscriptions, reconciliation and cleanup |
| cli.py | Persistent service options, private provisioning and resource cleanup |

SQLite stores hashed credentials, settings, requests, undirected accepted
friendships, per-user revision/last timestamp, current reports and consented
history. Each operation owns/closes its connection. WAL permits consistent reads
while writes serialize. Configuration persists across reopen; bool, non-finite,
nonpositive or Redis-TTL-unrepresentable durations are rejected. Existing tables
remain intact; current(expires) and friends(b,a) indexes are added in place.

## Report ordering, privacy and expiry

Both users must share location and mutually accept friendship. A location report
contains finite latitude/longitude/timestamp and optional device ID. Ordering is
per user across devices: the timestamp must strictly increase, be newer than
server_now−TTL, and no more than 30 seconds ahead. Crucially, server_now is sampled
after acquiring the SQLite writer. A report that becomes too old while queued
cannot acquire a renewed lifetime. Receipt time determines expiry; client skew
does not extend presence. History, current row and revision/timestamp commit or
roll back together. Returned dictionaries are independent parsed values.

Disabling sharing deletes the current row and advances the revision. Reenabling
requires a fresh timestamp/report; disconnect by itself preserves presence until
TTL. History consent is separate and off by default. Enabling affects future
reports; disabling deletes retained history. History reads are actor-only, even
if a legacy target field is present, and filter by retention (24h default).

Reports are read in one SQL transaction with one clock sample for both viewer and
friends. Expired state is logically hidden immediately. A background sweep runs
at min(30 seconds, TTL), retries transient failures and deletes expired current
and history rows; location updates no longer perform a global sweep. Physical
cleanup can lag under load or downtime. The durable single-current-row fallback
is an explicit extension to the book's ephemeral Redis cache; it is not an
unconsented sequence of history rows, and backups/disks still require protection.

## Redis is a bounded mirror, not the read authority

Accepted reports commit SQL first, then best-effort Lua cache warming and a
publisher-channel hint. A failed hint may be lost; there is no transactional
outbox or replay log. Lua prevents a lower revision replacing a higher one and
uses only remaining receipt TTL. Pub/Sub contains user ID hints, no coordinates.

A snapshot reads authorized SQL reports, performs one MGET and optional cache
repair, then reads SQL reports again after network work. The final consistent
read supplies every returned coordinate and rechecks TTL, location, friendship
and sharing. Redis does not decide output, and a cache hit does not avoid SQL
reads. This favors a clear local privacy/ordering authority over the book's
Redis-only fast path; a production Redis-authoritative design needs its own
revocation epochs/fences and failover rules.

Repair pipelines hold at most 128 reports each. The default 1-second repair
budget is checked between batches, not enforced by interrupting an active call.
An unavailable MGET ends optional repair rather than creating one timeout per
friend. Synchronous cache/broker clients have max_connections=16, 0.5-second
connect/socket budgets and zero configured automatic retries. Redis URLs accept
only a db query option, so URL options cannot override transport limits.

The backend caches at most 32 broker clients in an LRU. A publisher leases its
client; eviction closes only idle entries, outside the lifecycle lock. If every
entry is active at capacity, the optional hint is dropped. Backend.close rejects
new operations, drains accepted work and closes clients. These constraints do
not bound DNS or SQLite wall-clock work; closing can wait for such work to finish.

## Routing, subscriptions and delivery

A ring has 1..32 brokers, 64 virtual points per ID, and immutable owned nodes and
tokens. Endpoint replacement under the same broker ID preserves token placement.
An admin installs validated copied nodes through expected-epoch CAS. This is
local shared configuration with polling, not etcd/ZooKeeper consensus discovery.

Each session subscribes to itself and all accepted friends, including inactive
ones. Channels hash by publisher user ID. Each async broker-group subscriber
owns a separate one-connection client. Ring/friend changes retain unchanged
subscriptions, start replacements and retire affected tasks; reconnect marks the
session dirty. There is no no-gap subscription handoff guarantee. Periodic full
reconciliation (default 1s) repairs missed hints, expiry and revocation.

A new connection gets up to 20 currently nearby friends, distance, timestamp and
next_offset. A client without a valid own report sends its first location command
to seed presence. Explicit snapshot supports radius/offset/limit and returns
only that page without advancing the live delta baseline. A radius change is
compared with the previous set, preserving leave/enter/update events, including
changes outside the requested page. Clients merge pages and deltas by friend ID.
Deltas use frames of up to 100 changes; leave has ID only. Paging is
a live offset view: movement can shift pages, so clients deduplicate by ID.
Distance is spherical straight-line great-circle atan2, not road distance.

The final SQL read is the response authorization point. A revocation after that
read can race a prepared/in-flight frame; it cannot recall delivered data. Redis
recovery and reconciliation guarantee eventual repair, not lossless delivery or
instant revocation of already sent bytes.

## Protocol, admission and shutdown

Authorization must appear once in upgrade headers. Authentication runs before
initial application data. protocol.py accepts UTF-8 text JSON up to 16KiB,
rejecting duplicate keys, non-finite numbers, binary input and unknown operation
fields. request_id is optional, 1..64 characters. Outbound application frames
are capped at 256KiB; incoming WebSocket queue high-water mark is 16 and write
limit is 32768 bytes. Sends are serialized per session. A response or an entire
multi-frame delta batch shares a 3s deadline by default; a slow consumer is closed
rather than multiplying that budget by the number of frames.

The default 256 limit counts admitted upgraded application sessions, including
authentication. It is not a hard TCP/handshake quota. Opening timeout is 5s and
listen backlog is 128; ingress/load-balancer limits remain deployment work.
WorkPool defaults to 16 workers and submits no more than that many synchronous
SQL/Redis calls. Session waiters are bounded by admitted sessions, alongside owned maintenance
work. Cancelling an awaiter retains the capacity slot and waits for accepted work to exit before
propagating cancellation, avoiding orphan writes and resource-close races.

CLI exposes --max-connections, --workers and --send-timeout. Closing stops new
admission, closes sockets, retires subscriptions, cancels/drains the sweeper and
joins accepted synchronous tasks before the backend closes pools. Bind failures
do not start maintenance. DNS, SQLite and in-flight socket operations cannot all
be forcibly interrupted; these bounds do not promise a hard shutdown deadline.

Provisioning exclusively writes a fresh token file, flushes/fsyncs it, then
creates the SQL user. A failure removes only a newly created file whose identity
still matches. Existing files are never overwritten. File mode 0600 applies where
supported; Windows credential-folder ACLs remain an operational responsibility.

## Evidence and production boundaries

See [chapter review](chapter18-review.md) and the dated
[validation report](../benchmarks/results.md). Tests distinguish injected faults
from independent WebSocket/Redis process behavior. The lab creates per-run Redis
passwords and checks recorded run IDs before shutdown, avoiding adoption of an
unrelated service if a free-port race occurs. It retains isolated diagnostic
files under ignored .runtime/lab-* and stops its owned processes.

Shared SQLite serializes writes and every session still fetches its complete
friend set; the Redis mirror does not eliminate these SQL costs. No deployed
Cassandra/user sharding, Redis primary/standby, consensus discovery, cloud regions,
TLS termination, production identity/relationship service, load-balancer draining,
backup recovery or book-scale capacity is claimed. The book's 10M connections,
334K reports/s and roughly 14M forwarded updates/s remain architecture targets.
