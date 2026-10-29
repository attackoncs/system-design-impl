# Chapter 2 implementation requirements

Translate the book's architecture evolution — from a single machine to millions of users — into a runnable local HTTP experiment with 1/8/10 processes. Implement stateless web / shared persistent sessions, round-robin load balancing with request-level failure fallback, versioned profiles / CAS / idempotency, TTL/LRU cache, asynchronous read replicas, static account sharding, durable outbox with deduplicated consumer, local versioned edge-resource cache, and diagnostic counters.

Make eventual consistency, primary strong reads, and min-version reads explicit; do not equate TTL with the upper bound on replica lag. An existing database must refuse shard mapping changes without migration. Validate web/cache/replica failure, primary restart, shard isolation, and consumer ACK loss.

Do not claim unattended production failover, live migration, a real CDN or GeoDNS service, multi-region disk sync, production TLS/identity, or million-user capacity. Provide bilingual documentation, design notes, real tests, and measured reports. The local `GeoDNS` directory and explicit `Fence` promotion are lab models of the chapter's datacenter failover and slave promotion; they are not those production systems.

## 2026-10-27 review additions

Freeze inputs before SQL locks; validate single finite transaction timestamps. Bind replication to source identity and a complete contiguous bounded suffix, with atomic cursor/hash/profile updates. Fence ACK by token, original payload, lease expiry and source; scope consumer inbox IDs by source. Cache fills require generation tickets and retained version floors, with bounded bytes/metadata. Support1..8 configured web nodes and runtime pool updates, explicit edge invalidation/origin fallback, strict bounded HTTP/RPC and owned worker shutdown. Verify all with66 tests and raw network/performance records; do not promise bounded replica lag or primary promotion.
