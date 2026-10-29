# Requirements: S3-like Object Storage

Based on Chapter 25 of *System Design Interview.md* in the project root. Scope covers private buckets, object read/write, versioning and delete markers, sorted listing, multipart upload, small object aggregation, multi-replica storage, repair, and garbage collection. The book's 100 PB, six-nine durability, and four-nine availability numbers are unvalidated production targets.

1. Globally unique bucket names, static user identity isolation, versioning always enabled. Support version-specific read/remove and delete markers.
2. Data chunks up to 256 KiB with SHA256 validation; by default require at least 2-of-3 data-node durable acknowledgements before publishing full metadata.
3. Single PUT capped at 4 MiB; multipart upload up to 64 immutable parts, each up to 1 MiB, total object up to 64 MiB / 256 chunks. Validate total size and SHA256 before completion.
4. Persist parts and completion intent; support cross-API resume and idempotent completion after restart. Expire `active` sessions for cleanup, but always resume `completing` sessions instead of guessing they failed.
5. Chunks up to 64 KiB stored in aggregated pack files with a rebuildable offset index; support incomplete tail recovery and atomic generational compaction.
6. Metadata sharded by bucket UUID + key, merged and sorted across shards. Paginate with frozen results; HMAC-signed token binds user, bucket, and query parameters, valid for 5 minutes.
7. Read fallback, reference scan, replica repair, version retention, and unreferenced-chunk reclamation. Stop GC if any metadata source is missing.
8. Validated with real six-process HTTP tests for node exit, logical disk unavailability, byte corruption, publish interruption, listing changes, and recovery.

Scope excludes AWS signature/protocol compatibility, erasure coding, independent datacenter failure domains, metadata replication/leader election, online shard migration, or GB-scale streaming IO. A single directory being unavailable is not equivalent to a real hardware-failure experiment.

2026-10-26 review: owner bucket enumeration and delimiter/common-prefix listing;
signed frozen mixed pagination; strict privately owned manifests; bounded shared
reads/publications with exclusive GC/delete/list capture; per-upload completion
fencing; durable unknown-publication recovery and node generation-fenced sweep.
Overloaded, incomplete or corrupt state must fail closed. Validate delayed remote
requests as well as lost acknowledgements. Existing single-host limits and
unimplemented production HA/placement/GB streaming remain explicit exclusions.
