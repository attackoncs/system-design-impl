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

Live file heads carry a Python case-folded path key in a partial SQLite index on
(space, live_path_key). The write transaction probes the path and its ancestors
by equality and seeks descendants in the binary range [key + '/', key + '0').
Updating a head sets the key and deleting it clears the key. Initialization fills
keys from existing live heads when the column or Unicode version changes. Writers
sharing a database must use the same Unicode data version.

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
the logical quota. Retention never removes the current head. Retained bytes are a
`users.used` counter updated in the commit/prune transactions (backfilled once when
an older database is opened), so upload admission does not sum all revisions.

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
The scan skips files whose (size, mtime_ns) equal the stat last verified by hashing
(stored per file in client state; NULL after any remote update or for pre-upgrade
rows means hash); files modified within 2 s are always hashed, and explicit push
always rehashes. Same-size, same-mtime edits are the accepted stat-detection gap.
The reference assumes no hostile privileged local filesystem races. HTTP binds to
loopback; remote use needs TLS. The device cache defaults to a 64 MiB disk budget.

Pending/cancelled notifications never expose incomplete content. Idle long polls
only read; local conditions reduce latency and periodic reads cover cross-process
publications. Delivery is persisted once per returned batch, including a final ACL
check. HTTP separates long-poll admission from reserved foreground capacity, validates
framing/JSON, applies absolute socket deadlines and drains handlers/timers on close.
Service close wakes polls and waits accepted operations. SQLite/filesystem operations
can outlive socket deadlines. Metadata writes and conservative GC remain serialized.

Device transport uses bounded `read1` calls under one absolute socket deadline.
At EOF it verifies that no declared Content-Length bytes remain, even if the received
prefix is valid JSON or block data. Truncated fixed-length or chunked bodies raise
`OSError`, so automatic sync uses the existing capped I/O retry path. A failed
changes response cannot advance the local cursor or acknowledgement. Complete
close-delimited responses without Content-Length remain supported. Connections,
responses and deadline timers close on both success and failure.

### Event retention and re-snapshot

Policy: per workspace, events older than `event_max_age` (default 30 days) and rows
beyond `event_max_per_space` (default 100,000) are pruned; either bound may be
disabled. `event_spaces(space, floor, retained)` tracks a row counter and a floor.
Pruning only ever deletes an oldest prefix of one workspace (`events(space, seq)`
index, `DELETE ... WHERE space=? AND seq<=?`) and raises the floor in the same
transaction, so "seq <= floor" is exactly the pruned set. Every append prunes at most
`max(64, 2 * rows added)` rows of its own workspace (amortized, no count scans);
`cleanup`/`expire_events` sweeps workspaces that went idle. The log high-water mark
comes from `sqlite_sequence`, so cursors stay valid even if every row is pruned.
Old databases gain `events.created` (set to the open time) and per-space counters once.

Consistency: `changes` reads the high-water mark, the floors of the user's
workspaces and the events in one read transaction. If any floor exceeds the cursor
it returns no events, the same cursor and `resnapshot: true`; otherwise every event
after the cursor is still present in that snapshot, so no gap is delivered.
At startup the client reads `paged_snapshots` from `GET /config`. When available,
it sends `POST /snapshot` with a limit of 256. The first page records the starting
watermark H. An unfinished page returns `next: [space, path]`. The next request
sends that pair as `after` and echoes H as `cursor`. Each short transaction checks
membership and seeks through the existing (space, path) file index. After the last
page the service records delivery of H. The client persists H after applying all
pages, then acknowledges it. A failed page can be retried from the old cursor.
Updates during the scan are replayed after H. If retention passes H, the device
takes another snapshot. Clients that omit the limit receive the original single
response. A client connected to an older server also uses that format.

Reading ordinary metadata leaves idle long polls asleep. Committing an event wakes
local waiters. Periodic reads still pick up events committed by other processes,
and shutdown wakes waiting handlers so active operations can finish.

A failed block integrity check during download or commit flags the stored block
when its workspace, digest and declared size match. Open sessions report the digest
in `missing` until a verified upload clears the flag. A database upgrade initializes
existing blocks as unflagged. Upgrade metadata writers together because older binaries
use positional block inserts. A file tombstone applies only to a regular file at
that path. A directory can now contain other live files, and an empty local edit can
still differ from the last materialized version.

## Limits and validation

Fixed-size chunks reuse unchanged aligned blocks, not rsync-style byte shifting.
SQLite/local storage provide a runnable persistence reference; multi-host HA,
replicated object storage, production-scale load testing and mobile
watchers remain extensions. Simple uploads reuse the resumable state machine up to one block;
large files retain the manifest/session flow. File history supports an optional limit.

See [chapter audit](chapter16-review.md) for current executed evidence.

Tests cover encryption, interrupted upload, concurrent edits, rollback, corruption,
ACL revocation, quotas, event replay, safe GC and real HTTP/device processes. Reports
state fixture sizes, concurrency and actual transferred bytes.

References: [cryptography AEAD](https://cryptography.io/en/latest/hazmat/primitives/aead/),
[Python zlib](https://docs.python.org/3/library/zlib.html).
