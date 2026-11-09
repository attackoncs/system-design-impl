# S3-like storage reference design

## Processes and ownership

Six independent HTTP processes run in the lab: one catalog/API coordinator, two metadata shards and three data nodes. Metadata and chunk access cross HTTP; no shared Python objects stand in for replicas. Catalog owns private buckets, multipart sessions/parts and listing snapshots. Metadata shards own versions/manifests, request receipts and removal timestamps. Nodes own chunk files and disposable physical indexes.

Placement is `first_64_bits(SHA256(canonical_JSON([bucket_uuid,key]))) % shard_count`. Every version of a key stays on one metadata shard. Persisted topology rejects changing shard ID/count on an existing database. There is no online migration, automatic promotion or metadata replication.

The one coordinator uses shared ownership for ordinary operations and exclusive ownership for maintenance, bucket deletion and listing capture. Upload stripes and persistent publication intents protect staging and unknown results. A second active coordinator or direct writers bypassing it are unsupported. Multiple local processes are not independent physical disks/hosts/availability zones.

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

Chunks at most 64 KiB are appended to pack files; larger chunks have separate hash-named files. SQLite maps a hash to pack offset/length or a large-file path. A pack record stores magic, length, SHA256, creation time, payload and CRC32 over header/payload. PUT fsyncs the record before committing its index. An interrupted index commit leaves a safe orphan record recoverable on startup.

GC appends/fsyncs small-chunk delete tombstones before removing their index entries, preventing index rebuild from resurrecting collected data. Large-file PUT flushes and atomically replaces before indexing; recovery verifies names and hashes of unindexed files and sizes of indexed ones. Every GET also verifies SHA256.

`CURRENT` is the authoritative pack set: one name per line, sealed packs oldest first, then the single active pack that receives appends. Each chunk row stores its `pack`; indexes created before rotation get the column added and filled with CURRENT's only (legacy) pack in one transaction. Before an append would grow the active pack past `pack_limit` (`data_pack_bytes` in the config, default 256 MiB), it is rotated: inside one SQLite transaction the index is made to cover every byte of the pack (complete records past the watermark are applied), then the new empty pack is created and fsynced, CURRENT is atomically replaced (temp file fsync, rename, directory fsync), and `sealed_packs(name,size)` plus the new `pack_state` commit together; finally the sealed pack is made read-only. Invariant: a sealed pack is fully indexed and never changes again. Reads open the pack named by the row; tombstones always go to the active pack, also for chunks in sealed packs.

SQLite also stores `pack_state` (active pack name, start and end of the last indexed record). It advances only in the same transaction as the chunk-row change of an already fsynced record that directly follows the previous end, so every byte before that end is durable and reflected in the index. Startup checks that `sealed_packs` equals CURRENT's sealed list (names, order and file sizes), that the stored pack is CURRENT's active pack and that the record ending at the stored end parses with a valid CRC, then scans/hashes only the active pack's records after it (orphans left by a crash between append and index commit) and truncates only an incomplete final record. Any mismatch (old index, restored file, a crash inside rotation or compaction) falls back to the full rebuild, which explicit `recover()` and compaction always use: it replays every listed pack in CURRENT order, so later tombstones and re-uploads win; a truncated sealed pack fails closed. Files not listed in CURRENT are leftovers of an interrupted rotation/compaction and are deleted. Crash points: before the CURRENT replace, the old layout is intact (the new empty file is unlisted); after it but before the index commit, CURRENT no longer matches `sealed_packs`/`pack_state`, forcing a full rebuild of the new layout. Indexed large files get a size check at startup; unindexed ones are hashed and indexed, and rows whose file vanished are dropped. Complete bad-CRC records remain unreachable; replicas can repair. Invalid magic/length fails closed for operator recovery instead of guessing a new boundary. Index loss does not lose valid pack data.

Compaction holds the node lock, seals the non-empty active pack first (so it never rewrites a pack receiving appends), writes/fsyncs the live indexed small chunks of all sealed packs into new sealed packs of at most `pack_limit` bytes, publishes CURRENT as those packs plus the new empty active pack, rebuilds the index and deletes the unlisted inputs. Merging all sealed packs together makes dropping their tombstones safe: no older copy of a collected chunk remains. A corrupt live chunk aborts compaction until repaired. Before the CURRENT switch, a restart uses the old packs; afterward the new ones. Tests verify retained-disk rebuild/tail/tombstone/compaction behavior; rotation and compaction crash points were checked by a throwaway directory-snapshot script, not every power-loss/filesystem behavior.

## Listing snapshots

The coordinator merges latest visible rows (or all live versions) from every metadata shard, sorts keys and freezes at most 10,000 rows in the catalog. The coordinator excludes its own mutations during capture; there is no distributed SQL transaction or independent metadata writer support.

HMAC-SHA256 tokens encode snapshot ID/index and bind server-side state to owner, bucket UUID, prefix, versions mode and page size. Tokens expire after five minutes. Subsequent pages use frozen rows despite additions/deletion markers. They guarantee stable enumeration, not retained downloads: explicitly removing a version can make its saved listing entry unreadable. Missing shards fail listing instead of returning a partial result.

## Scrub, repair and GC

Reads resolve the manifest and try nodes until a verified copy of each chunk is found, then verify full length/hash. No good copy returns 503, not altered data. Reads do not assume repair occurred.

Administrative maintenance resumes completions, expires active sessions, then gathers all metadata references: live versions and recently removed versions within grace. Active/completing uploads add references. Missing any metadata source stops before sweep. A 50,000-unique-chunk budget bounds admin requests/memory and fails explicitly.

Scrubbing checks each live hash on every node, repairs missing/corrupt copies from a good source and reports repaired copies, failed writes and unrecoverable hashes. It then asks nodes to collect unreferenced chunks older than grace (default 60 seconds, tests explicitly use zero). Coordinator exclusion, durable publication intents and node collection generations protect mark-and-sweep, including delayed remote handlers. Node locks protect IO/compaction/collection. Shared chunks remain while any version/upload references them. The implementation is a bounded full scan, not a distributed production collector.

Replication normally costs about 3x byte storage. Erasure coding could reduce space but needs encoding, multiple available fragments and reconstruction bandwidth. No erasure codec or independent failure-domain test is implemented. Losing every verified copy is unrecoverable.

## Security and scope

Static users own private buckets. Internal/admin tokens and the continuation signing secret are distinct. Users cannot directly access chunk/metadata endpoints. Servers bind loopback only. No S3 Signature V4, IAM policy, TLS, encryption, public buckets, range reads or presigned URL compatibility is claimed.

Catalog and metadata are single-owner SQLite failure domains, and repair is explicitly scheduled. Tests retain disks or simulate a missing node directory while preserving its original bytes. No automatic election, autonomous multi-host repair, online resharding, production lifecycle/archival or SLA exists. The book's 100 PB, six-nine durability and four-nine availability targets remain unverified.

## Chapter 25 engineering review (2026-10-26)

Added `GET /v1/buckets` for the authenticated owner's buckets and
`GET /v1/buckets/{bucket}/objects?delimiter=/` for same-level objects and
`common_prefixes`. The default `delimiter=` remains recursive. Prefixes and
objects share the page limit. Continue with identical owner/bucket/prefix,
versions/limit/delimiter; signed tokens expire after five minutes. New writes do
not change an existing snapshot. The catalog automatically migrates the old
snapshot table and supports old recursive continuation tokens.

Bounds: 1000 buckets per owner; 10,000 raw rows across metadata shards before
prefix filtering; 128 active snapshots globally/32 per owner, 8 MiB per snapshot.
A single-page listing does not persist a snapshot. Exceeding a budget is an
explicit error, not a partial listing.

Catalog, listing, record contracts, chunk I/O, coordinator runtime, routes and
transport are separate modules. The single coordinator admits eight operations
and owns twelve replication workers; HTTP admission is bounded to 32 requests.
Ordinary object reads/publications may overlap. GC, bucket deletion and listing
capture take exclusive ownership; multipart operations use 64 upload lock
stripes. Publication/quorum, immutable parts, durable completing state and
version idempotency remain enforced. Data nodes share read ownership and give
writers priority. Uncertain append/index/CURRENT failures fence the node with
`needs_recovery` until a verified recovery succeeds.

A local timeout does not prove that a remote handler stopped. Therefore every
metadata publication first records a durable catalog intent. An unknown result
remains pending; maintenance replays all pending publications before gathering
references. Over 128 pending intents, corrupt intents or failed recovery stop
collection. Intent completion occurs only after a verified metadata receipt.
Old catalogs without the new intent history cannot retroactively recover
unknown operations from before this upgrade; upgrade with old requests drained.

Maintenance captures each node's `gc_generation` before scanning references.
Every accepted PUT, including a duplicate chunk, changes that generation;
recovery, compaction and collection also invalidate it. Internal HTTP `/collect`
requires `{live,grace,generation}` and validates generation after acquiring the
node writer lock. A delayed stale request returns409 before deleting data.
Repair writes can invalidate the current sweep: `collection_deferred` means
repeat maintenance with a fresh complete snapshot. No generation refresh is
silently used to accept an old reference set. Node restarts use fresh random
generations, avoiding dependence on synchronized cross-machine wall clocks.
The local direct `DataNode.collect()` API retains its trusted unfenced form for
compatibility; external callers must use the fenced coordinator protocol.

Direct HTTP bypasses proxies and redirects, validates framing/JSON/checksums and
uses shared deadlines/cancellation. Ordinary API requests use a 15-second total
budget; maintenance uses60 seconds; each RPC defaults to at most5 seconds.
Shutdown drains accepted work and closes the shared pool. Arbitrary SQL/CPU/DNS
or custom blocking RPC implementations cannot be forcibly terminated.

Final validation: **137 tests passed,40.784 seconds**, no skips, `-W error`.
Both six-process demos passed. Four-client measurement:40 verified objects,
38.23 PUT/s and76.98 GET/s; PUT p50/p95=99.655/124.262ms,
GET p50/p95=48.046/69.440ms. This small localhost burst is not a controlled
speedup comparison or a production SLA. See [chapter review](chapter25-review.md)
and [measurements](../benchmarks/results.md).

Remaining chapter-scale work: placement heartbeats/consensus, independent AZs,
metadata/catalog HA, per-core append streams (pack rotation is implemented), streaming GB
objects, erasure coding, automatic repair scheduling, online resharding, cloud
IAM/TLS/encryption and lifecycle/backup/100PB durability validation. No real AWS
service was used, and no commit or push was made.

Bucket deletion also rejects pending publication intents with409, preventing
recovery from publishing an inaccessible version into an already removed bucket.

## Upstream error-response review (2026-11-05)

The chapter and pinned open-source implementations were compared with the local
metadata, catalog, listing, multipart and publication paths; see the
[source audit](open-source-review_cn.md). The confirmed defect was in HTTP error
translation: decoding a deeply nested JSON body from an upstream `HTTPError`
could raise `RecursionError` while handling that error, closing the connection
without an HTTP response. The minimal fix catches it alongside malformed JSON,
duplicate fields and non-finite values, returning a neutral 503 response instead.
An ordinary valid upstream 409 remains 409; deeply nested client request JSON
remains a client error (400). The neutral fallback does not expose the invalid
peer error body.

This change does not retry writes automatically or alter metadata/data concurrency.
A publication with an unknown result retains its catalog intent for later
idempotent recovery. The new regression uses a real Service, Catalog and HTTP API
with a controlled metadata peer: deleting an object sends exactly one `/publish`
request for its marker. That peer does not actually commit metadata, so this test
is not evidence of recovery after a real lost publication acknowledgement.

Baseline: 137 tests in 41.699 seconds. Final: **138 passed in 41.769 seconds**,
with `-W error` and no skips. The HTTP suite has 20 tests (2.422 seconds), including
one new test with six subcases; before the fix its deeply nested 409/503 cases
failed with a connection error. These timings are observations, not a controlled
speedup comparison. Earlier 2026-10-26 measurements remain historical. See the
[validation report](../benchmarks/results.md) for current evidence and unchanged
production limitations.
