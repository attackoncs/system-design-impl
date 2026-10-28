# Design: File Sync System

## Components and consistency

Authenticated HTTP API, block store, metadata state machine and device client are
separate modules. SQLite WAL holds identities, workspace ACLs, files, immutable
revisions, resumable sessions, blocks/reference counts and durable user events.
BEGIN IMMEDIATE serializes transitions across reference server processes. No
mutable metadata cache hides writes or ACL changes. Immutable manifests are cached
by revision with a 128-entry/8 MiB encoded-input budget; current ACL/head is queried.
Long polling uses the durable
event log and periodic database reads, so lost wake-ups cannot lose changes.

## Upload and revisions

The client hashes fixed-size chunks and declares their ordered digest/size manifest.
A session ID and caller request ID identify retryable work. Same-workspace blocks
can be reused, while unrelated workspaces cannot probe each other's hashes. Block
bytes are checked, compressed and encrypted before their metadata is committed.
Content verification runs outside the metadata lock against immutable session input.
The final transaction rechecks the session, ACL, block metadata/existence and reserved
quota; revision, head, references and notifications commit together.
Incomplete uploads never appear as downloadable files.

The expected base revision fences edits. First matching publication wins. A stale
writer receives a separate conflict file, with a server-generated unique path;
its session records that result so a retry cannot create multiple copies. A stale
delete is rejected. Revisions and tombstones remain available until explicit
retention pruning. Quota counts logical bytes across retained revisions, including
conflicts, plus open upload reservations; physical dedup savings do not increase
the logical quota. Retention never removes the current head.

## Storage and security

Maximum block size is 4 MiB by default. zlib compression precedes AES-256-GCM from
cryptography; every write gets a random 96-bit nonce. Per-workspace keys are derived
using HKDF; associated data binds workspace, plaintext SHA-256 and size. Decompression
is bounded and the plaintext digest is checked. The persisted master key needs
protected backups; production KMS and rotation require a separate design.

Object names are server-derived opaque hashes. Atomic temporary writes and rename
precede metadata publication; failed metadata commits leave collectible orphans.
Reference counts track every retained revision. Open sessions pin declared blocks.
GC takes a metadata transaction and grace period before deleting unreferenced data.
Upload expiry commits first, so a failed physical sweep cannot restore expired
reservations. Shared per-key process guards serialize block reads/publish/tiering/GC.
Ciphertext reads and decompression are bounded; verified duplicate writes preserve
open reader inodes. Grace is not a durable pin for paused, uncommitted work.
A local cold directory demonstrates explicit archive/restore semantics; it does not
provide cloud durability or Glacier pricing/latency.

## Synchronization and notifications

Events carry immutable revision IDs, so a paginated feed can replay precise states.
Each device has acknowledged and delivered cursors. Acknowledgement is monotonic and
cannot exceed delivery. Clients persist local progress only after filesystem work
succeeds for the entire page, then acknowledge; retries can replay events. Download cache keys include
workspace and digest. Local dirty files are copied to a conflict path before remote
replacement; local deletions become conditional server tombstones.

Only authenticated members can read metadata or blocks referenced by their files.
ACLs are checked on every request. Revocation prevents subsequent server reads but
cannot erase previously downloaded data. Paths reject traversal, platform-reserved
names and symlinks; the client checks containment and reserves its state directory.
An OS ownership lock enforces one client process per device folder. Automatic sync
binds one workspace, has a directory-entry limit and capped failure backoff; protected
conflict copies remain local. This is polling/scanning, not a native OS watcher.
The reference assumes no hostile privileged local filesystem races. HTTP binds to
loopback; remote use needs TLS. The device cache defaults to a 64 MiB disk budget.

Pending/cancelled notifications never expose incomplete content. Idle long polls
only read; local conditions reduce latency and periodic reads cover cross-process
publications. Delivery is persisted once per returned batch, including a final ACL
check. HTTP separates long-poll admission from reserved foreground capacity, validates
framing/JSON, applies absolute socket deadlines and drains handlers/timers on close.
Service close wakes polls and waits accepted operations. SQLite/filesystem operations
can outlive socket deadlines. Metadata writes and conservative GC remain serialized.

## Limits and validation

Fixed-size chunks reuse unchanged aligned blocks, not rsync-style byte shifting.
SQLite/local storage provide a runnable persistence reference; multi-host HA,
replicated object storage, bounded production event retention and mobile watchers
remain extensions. Simple uploads reuse the resumable state machine up to one block;
large files retain the manifest/session flow. File history supports an optional limit.

See [chapter audit](chapter16-review.md) for current executed evidence.

Tests cover encryption, interrupted upload, concurrent edits, rollback, corruption,
ACL revocation, quotas, event replay, safe GC and real HTTP/device processes. Reports
state fixture sizes, concurrency and actual transferred bytes.

References: [cryptography AEAD](https://cryptography.io/en/latest/hazmat/primitives/aead/),
[Python zlib](https://docs.python.org/3/library/zlib.html).
