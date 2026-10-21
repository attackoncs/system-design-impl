# S3-like storage reference design

## Processes and ownership

Six independent HTTP processes run in the lab: one catalog/API coordinator, two metadata shards and three data nodes. Metadata and chunk access cross HTTP; no shared Python objects stand in for replicas. Catalog owns private buckets, multipart sessions/parts and listing snapshots. Metadata shards own versions/manifests, request receipts and removal timestamps. Nodes own chunk files and disposable physical indexes.

Placement is `first_64_bits(SHA256(canonical_JSON([bucket_uuid,key]))) % shard_count`. Every version of a key stays on one metadata shard. Persisted topology rejects changing shard ID/count on an existing database. There is no online migration, automatic promotion or metadata replication.

The one coordinator serializes operations and maintenance with a process lock. This protects publication, bucket deletion, part staging and GC from racing, and limits throughput. A second active coordinator or direct writers bypassing it are unsupported. Multiple local processes are not independent physical disks/hosts/availability zones.

## Versions and publication

Private bucket names are globally unique. Recreated names get new UUIDs. Keys are flat names up to 512 UTF-8 bytes, including slashes/Unicode, and never select local paths. Bucket versioning is always on.

Each PUT stages SHA256-addressed chunks (maximum 256 KiB), attempts all three data nodes concurrently, and requires two durable acknowledgements per chunk by default; configuration may require three. Only then does one metadata transaction publish the complete ordered manifest, total SHA256/length and a new UUID version. Failed writes leave collectible staging chunks and no visible partial object. Two acknowledgements do not claim three immediately healthy copies. Empty objects need no data chunks.

Idempotency receipts are unique by `(bucket,key,request_key)`. Same manifest/content returns the original version; changed content fails 409. A receipt for a permanently removed version cannot recreate it. Single PUT is bounded to 4 MiB.

DELETE without a version publishes a delete marker. Latest reads/listing hide that key, but old version reads remain available. Removing a marker reveals the previous version. Exact-version deletion marks it removed, and retention marks all but the newest N live versions per key removed (markers count). Grace protects recently removed bytes. Bucket deletion requires no live versions/markers or active/completing sessions. Removed rows and receipts are retained for deduplication rather than automatically purged.

## Multipart state machine

Initiation binds bucket, key, request key, total size/hash and a one-hour deadline. Parts 1–64 are immutable, 1 byte–1 MiB each, and individually SHA256 checked. Accepted parts have met the data write threshold and have a persisted manifest. Same intent/part bytes replay safely. Parts remain invisible as objects.

Completion requires contiguous parts starting at one, no more than 256 chunks/64 MiB, and checks the entire ordered content against expected length/hash. It writes a durable `completing` intent before cross-service publication. Frozen parts cannot change or be aborted after that point. Publishing uses the original request key; retry or maintenance safely resumes after a crash before publication or between publication and the catalog's `complete` result commit. No second version is created.

Expiry only aborts `active` sessions. Maintenance resumes `completing` first and never assumes its original deadline means publication failed. Active/completing part references pin their bytes; completed-session history alone does not pin data after its version is removed. Reads assemble a bounded object in memory; this is not GB-scale streaming.

## Packed small chunks

Chunks at most 64 KiB share an append pack file; larger chunks have separate hash-named files. SQLite maps a hash to pack offset/length or a large-file path. A pack record stores magic, length, SHA256, creation time, payload and CRC32 over header/payload. PUT fsyncs the record before committing its index. An interrupted index commit leaves a safe orphan record recoverable on startup.

GC appends/fsyncs small-chunk delete tombstones before removing their index entries, preventing index rebuild from resurrecting collected data. Large-file PUT flushes and atomically replaces before indexing; recovery verifies file names/content hashes. Every GET also verifies SHA256.

`CURRENT` selects the authoritative pack generation. Startup rebuilds the index, truncating only an incomplete final record. Complete bad-CRC records remain unreachable; replicas can repair. Invalid magic/length fails closed for operator recovery instead of guessing a new boundary. Index loss does not lose valid pack data.

Compaction holds the node lock, writes/fsyncs live indexed small chunks into a new generation, atomically switches CURRENT, rebuilds the index and removes old generations. A corrupt live chunk aborts compaction until repaired. Before the pointer switch, a restart uses the old pack; afterward it uses the new pack. Tests verify retained-disk rebuild/tail/tombstone/compaction behavior, not every power-loss/filesystem crash point.

## Listing snapshots

The coordinator merges latest visible rows (or all live versions) from every metadata shard, sorts keys and freezes at most 10,000 rows in the catalog. The coordinator excludes its own mutations during capture; there is no distributed SQL transaction or independent metadata writer support.

HMAC-SHA256 tokens encode snapshot ID/index and bind server-side state to owner, bucket UUID, prefix, versions mode and page size. Tokens expire after five minutes. Subsequent pages use frozen rows despite additions/deletion markers. They guarantee stable enumeration, not retained downloads: explicitly removing a version can make its saved listing entry unreadable. Missing shards fail listing instead of returning a partial result.

## Scrub, repair and GC

Reads resolve the manifest and try nodes until a verified copy of each chunk is found, then verify full length/hash. No good copy returns 503, not altered data. Reads do not assume repair occurred.

Administrative maintenance resumes completions, expires active sessions, then gathers all metadata references: live versions and recently removed versions within grace. Active/completing uploads add references. Missing any metadata source stops before sweep. A 50,000-unique-chunk budget bounds admin requests/memory and fails explicitly.

Scrubbing checks each live hash on every node, repairs missing/corrupt copies from a good source and reports repaired copies, failed writes and unrecoverable hashes. It then asks nodes to collect unreferenced chunks older than grace (default 60 seconds, tests explicitly use zero). Coordinator serialization prevents in-flight upload publication from racing this mark-and-sweep. Node locks protect IO/compaction/collection. Shared chunks remain while any version/upload references them. The implementation is a bounded full scan, not a distributed production collector.

Replication normally costs about 3x byte storage. Erasure coding could reduce space but needs encoding, multiple available fragments and reconstruction bandwidth. No erasure codec or independent failure-domain test is implemented. Losing every verified copy is unrecoverable.

## Security and scope

Static users own private buckets. Internal/admin tokens and the continuation signing secret are distinct. Users cannot directly access chunk/metadata endpoints. Servers bind loopback only. No S3 Signature V4, IAM policy, TLS, encryption, public buckets, range reads or presigned URL compatibility is claimed.

Catalog and metadata are single-owner SQLite failure domains, and repair is explicitly scheduled. Tests retain disks or simulate a missing node directory while preserving its original bytes. No automatic election, autonomous multi-host repair, online resharding, production lifecycle/archival or SLA exists. The book's 100 PB, six-nine durability and four-nine availability targets remain unverified.
