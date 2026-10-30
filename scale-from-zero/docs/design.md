# Chapter 2 — Staged scaling laboratory

## Educational scope

The source chapter moves from a single server to stateless web scaling, database replication, cache, CDN, multiple data centers, queues and data sharding. This implementation makes a bounded subset executable with real local HTTP, while distinguishing models and unimplemented deployment capabilities. It does not assert millions of active users, cloud/CDN integration, multi-region replication or production availability.

```mermaid
flowchart LR
    U[Session client] --> L[Round-robin balancer]
    L --> W0[Stateless Web 0]
    L --> W1[Stateless Web 1]
    W0 --> C[TTL/LRU cache]
    W1 --> C
    W0 --> P0[(Primary shard 0)]
    W1 --> P1[(Primary shard 1)]
    P0 -->|HTTP change pull| R0[(Profile replica 0)]
    P1 -->|HTTP change pull| R1[(Profile replica 1)]
    W0 --> R0
    W1 --> R1
    J[Worker] -->|Claim/ACK| P0
    J -->|Claim/ACK| P1
    J --> I[(Deduplicated event effects)]
    E[Local edge-cache model] -->|Versioned static objects| L
```

The `single` stage runs app/database and a consumer thread in one process. `balanced` has eight service processes with one primary/replica pair; `sharded` has ten with two pairs. Each lab uses independent files, private ports and only owned child cleanup. Transitions are separate deployments, not live migration of the same data directory. Vertical hardware resizing and autoscaling remain design discussion. Geographic failover and replica promotion exist as the local models in `regions.py` and `leadership.py`; they are not a DNS deployment or automatic failover of the 1/8/10-process stages.

## Request routing and sessions

Web nodes own no durable profiles or sessions. Static generated synthetic login credentials are shared configuration, while bearer session hashes/user/expiry live on the routed primary. Tokens carry a validated username prefix for routing and a high-entropy opaque suffix; the suffix is never trusted without a primary lookup. Expiry is one hour. This demonstrates session-independent web routing, not secure production identity lifecycle, password storage, TLS, cookie/CSRF policy or KYC.

The load balancer rotates initial web choice and retries another web on request-time failures. GET is safe to retry; profile mutations carry stable request keys; login is not automatically retried. It forwards no admin/internal routes. This is not a background health/election controller. A primary/session outage can make both web nodes fail for the affected user, which adding web replicas does not fix.

## Primary transactions and static sharding

User routing is SHA256(user) modulo configured primary count. A data file records its count/index and refuses changed topology, preventing silent in-place rehashing. There is no online migration or dynamic membership. User names in the demonstration deliberately route to separate shards; all user writes/reads/sessions use the same mapping.

`users` stores a versioned synthetic profile. `requests` persists per-user command hashes and original results. A SQLite `BEGIN IMMEDIATE` transaction checks expected version and idempotency, updates the profile, appends a change and enqueues a stable outbox event. WAL/FULL synchronous settings preserve local commits. Duplicate identical keys return the original result; changed content or stale expected version is409. Two different concurrent keys using the same expected version cannot both overwrite the profile.

The change stream contains contiguous shard-local sequence, user, new version/profile and checksum. Replica pull applies change and cursor atomically, rejects gaps/corruption/conflicting replay, and ignores identical duplicates. Replicas preserve changes/profile state only; they do not hold primary session/request/outbox state. Promoting one requires an explicit `Fence` and still does not move sessions or jobs. Profile history is bounded to1000 mutations per primary in this reference.

## Cache and read consistency

Shared cache is a separate volatile process with32-entry LRU and2-second monotonic TTL. Cache values are copied on get/put to avoid object mutation leakage. Cache-aside reads try cache, then matching read replica, then primary on errors/missing profile. Writes commit before best-effort invalidation; cache failure never rolls back a successful write. Cache restart loses derived data only.

Eventual reads can be stale. Generation tickets reject fills started before an observed invalidation; retained version floors reject values older than observed writes. Failed invalidation, restart or floor eviction can still permit stale replica refills. Therefore the TTL is not a staleness SLA. `min_version` filters cache/replica versions and falls back to primary; `consistency=strong` bypasses both. A required version newer than the primary returns409. No cross-shard atomic read is offered. Session authentication always contacts the primary even for a cache hit, so cache hit reductions describe **profile** queries, not all primary traffic.

Failure of the read replica routes profile reads to the primary. Failure of the primary does not promote a replica inside `App`; promotion happens only when a `Fence` is attached and `/internal/promote` succeeds. Other shards continue with their own primaries, and worker polling isolates per-shard transport failures. There is no cross-host replication, distributed transaction or independent failure-domain guarantee.

## Durable asynchronous work

Profile change and outbox insertion commit together. A worker claims tasks using a five-second lease plus fresh token, persists a deduplicated effect in its own inbox, then acknowledges the source. Reclaiming after a lost ACK uses the same event ID; repeating delivery increments attempt count but not effect count. Old claim tokens cannot acknowledge a newer claim. In the single-process stage, a local thread uses the same protocol without HTTP.

`lose_ack` fault control deliberately suppresses acknowledgement after recording the effect. Restart recovers the existing inbox and source lease. Effects are synthetic notification/audit records, not real outbound messages. The queue retries indefinitely; it has no dead-letter policy, fairness guarantee, broker replication or ordering guarantee across versions/shards. This is a durable outbox exercise, not the later distributed-message-queue implementation.

## Local edge model and diagnostics

Static origin assets have versioned path identities. A separate local edge process caches their JSON content for30 seconds; edge loss can be handled by explicitly reading origin. There is no provider API, GeoDNS, global POP, TLS, byte-range HTTP cache or browser DNS fallback. The label on edge output explicitly identifies the model.

Per-process diagnostic counters expose balancer fallback, web identity, primary profile reads, replica cursor, cache hits/misses/evictions and job delivery state. Counters are instructional, mostly ephemeral. They are not comprehensive monitoring, tracing, alerts or capacity automation. Benchmark stages are measured separately without a test load; adding extra HTTP hops at tiny local scale may lower throughput even while reducing primary profile reads.

## Validation and production extensions

Sixty-six tests cover concurrent idempotency/CAS, session persistence/expiry, replication gap/checksum/replay, LRU/TTL/version reads, lease fencing, topology-change refusal, actual1/8/10-process requests, web/cache/replica failures, primary restart/shard isolation, consumer ACK loss and edge-origin access. See `../benchmarks/REPORT.md` for final commands and measurements.

The experiment is bounded and one-host. Primaries, session dependencies, balancer, consumer and cache/edge each retain individual failure modes. Production extensions require real database HA and recovery fencing, replicated sessions, load-balancer redundancy, real CDN/GeoDNS, regional synchronization, migration protocols, security, independent monitoring and relevant long-running capacity/failure tests. The local sample cannot establish any of those properties by process count alone.

## Engineering organization (2026-10-23)

Routing policy lives in balancer.py; background single-step operations and owned-thread shutdown live in workers.py. Cache owns its lock, copies snapshots outside the metadata lock, and no longer requires HTTP callers to synchronize it. Profile POST retries require its idempotency key; login is never replayed merely because its body contains request_id. Eight additional regressions cover these contracts.

## Protocol after the 2026-10-27 review

Current behavior is defined here and in the README. Contracts freeze values; profiles/sessions/jobs/replication/consumer/schema isolate persistence; runtime/routes/transport/server_runtime isolate I/O/lifetimes. Old imports remain; no cross-chapter runtime dependencies.

Cache probes return instance-generation tickets and observed-version floors. Invalidation advances generation even with no value; fills verify the ticket. Values are bounded32/512 KiB, floors1000. Best-effort invalidation failures, restart or floor eviction can still allow old replica data: TTL does not bound replica lag. A read begun concurrently may return an older snapshot, but cannot refill from a pre-invalidation ticket; subsequent reads honor retained floors. Strong/min-version semantics remain.

Persistent database/source identities survive restart. Feeds carry after/previous_hash/count/head/source_id and validate the complete bounded suffix/per-user versions before atomic batch/binding commit. History is immutable. This is not database HA; legacy initial binding/inbox source cannot be authenticated retrospectively. Stop/back up and upgrade services together.

Workers use5-second leases, with one finite clock sample after writer ownership. ACK checks actual expiry/token/payload/source; consumer IDs include source identity. Each service HTTP32/15 seconds,64 KiB/8 MiB; bounded RPCs inherit deadlines. SQL/CPU cannot be force-stopped; ownership drains. Non-daemon workers start after binding, stop/close/join even on close failure.

Web pools can add/remove1–8 configured nodes online without holding the routing lock over I/O; restart restores config, and DB shards remain static. Edge invalidation and explicit client-origin fallback are local models. Online migration and production capacity are unimplemented. See [chapter review](chapter02-review.md).

## Local datacenter and promotion model (2026-10-29)

`regions.GeoDNS` is the chapter's traffic redirect: a location resolves to its configured center, and an unhealthy center yields the next healthy one. With two centers, an outage sends every resolve to the survivor. It does not query DNS, shift a percentage of live packets, or replicate storage. Point `replicate_once` at the remote primary for the chapter's asynchronous copy.

`leadership.Fence` is the chapter's "promote a slave when the master is offline" step, bounded to profile rows. The shared lease publishes the primary cursor. Promotion waits until the lease expires and refuses a replica that has not caught up, which is the chapter's warning that a slave can be missing data and needs recovery before it becomes master. The promoted process accepts `/internal/update` only. Sessions, idempotency keys, and outbox jobs are not on the replica, so authentication and background work do not move. The default stages leave `Store.fence` unset and keep replicas read-only.

Cache eviction follows the chapter's LRU default, with LFU and FIFO as explicit policies. `set`/`get` cover the chapter's text cache call. `CacheCluster` hides one member failure. The running lab still uses one cache process unless a caller builds a cluster. None of this is multi-region HA or a million-user run. The suite is 73 tests.
