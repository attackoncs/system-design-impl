# Design: Nearby Friends

## State and permissions
Two independent WebSocket servers share SQLite WAL reference metadata and Redis.
SQLite transactions store hashed credentials, users/sharing/history consent,
mutually accepted undirected edges, requests, monotonically increasing per-user
revision, current reports and consented history. One accepted timestamp sequence
per user prevents a delayed second device from moving location backwards.
The durable current row is a local recovery extension to the book's ephemeral
cache; rows are purged after inactivity. It is not location history without consent.

Each accepted report commits authoritative state before best-effort Redis cache
and Pub/Sub publication. Redis Lua only replaces cache revisions monotonically;
TTL is remaining server-receipt lifetime. Snapshots fetch cache in a batch, check
revision against authoritative metadata and fall back to current state if cache
is missing/stale. Therefore cache outages may lose hints but do not authorize old
coordinates. Privacy generation is represented by the monotonic revision; sharing
off removes current state and increments revision. Reenable requires a new report.
History disabled by default; enabling affects future reports only. Disabling
history deletes retained history; configurable retention is swept periodically.

## Delivery
Bearer authentication is in the WebSocket upgrade headers; unauthenticated peers
are closed before application data. Per-connection queues and frame size are
bounded by the WebSocket library. A session subscribes to its own and all friends'
channels, including inactive friends. Broker connection errors cause retries.
Channels use publisher user ID and a SHA-256 ring with 64 virtual nodes per broker;
broker IDs, not endpoint addresses, determine token placement. An admin replaces
the persisted ring with an expected-epoch CAS. This local shared configuration
substitutes for etcd/ZooKeeper and does not claim consensus-backed discovery.

On ring/friend changes sessions cancel old subscriptions, attach new ones and
reconcile from current state. Pub/Sub events contain only user/revision hints,
never coordinates. Receivers recompute their own full authorized nearby set.
An independent periodic reconciliation (default 1s) repairs lost events, TTL
expiry and permission changes. Sends are serialized per connection. Initial
snapshot is paginated at 20, while enter/update/leave deltas cover the complete
set; leave contains ID only. Explicit snapshot paging is a live offset view, not
an immutable cursor, so moving friends may shift pages. Clients should deduplicate
IDs or request page zero again. Snapshot and payload generation are one SQL read
transaction; revocation may race an already-in-flight frame but is applied on the
next reconciliation. No claim of retracting already delivered data.

## Limits and operations
Default radius 5mi, maximum 100km; default expiry 600s. Client timestamps determine
ordering but server receipt determines TTL, so client future clock skew cannot
extend lifetime. Shared SQLite serializes writes; Redis cache/publish follows SQL
commit without a transactional outbox. Failed publication is repaired by periodic
snapshot reads; rediscovery cache warming is demand-driven. SQLite is not suitable
for the book's write volume. Self-history API and explicit forgetting implement
data minimization; periodic sweeping is opportunistic during active service.
Production needs TLS, external identity/relationship service, distributed SQL,
region-local caches, discovery HA, backups, telemetry, load balancing/draining,
clock monitoring and larger sustained-load validation.
