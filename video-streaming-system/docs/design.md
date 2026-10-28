# Design: Video Streaming System

## Data flow

```text
client -> authenticated API -> expiring part grants -> original object storage
                               |                            |
                         video metadata               durable DAG jobs
                                                            |
                         inspect -> [GOP preprocessing] -> rendition(s) + [thumbnail]
                                                            |
                                                         publish
                                                            |
                                                  completion outbox
                                                            |
                                    handler -> READY metadata + encoded object map
                                                            |
                          playback grant -> bounded edge cache -> HLS/segments/key
```

The original chapter's upload, metadata, transcode and completion paths are separate.
SQLite stores credentials, grants' signing secret, upload parts, video state, jobs,
per-job attempt limits, leases and completion outbox. Content-addressed files are published atomically
and fsync before metadata references them. API/worker processes share local storage;
real multi-host deployments need a replicated database and object storage adapters.

## Upload and authorization

Owner credentials are provisioned out of band. Upload requests declare total bytes
and fixed part size; an HMAC grant binds each part's video, index, expected length
and expiration. Full parts can be retried idempotently; conflicting bytes fail.
Finalization streams parts into a source object and checks total/full SHA256 before
atomically scheduling the DAG. An interrupted finalization can leave an unreferenced
object, but cannot expose a ready video. Owners cannot finalize other owners' uploads.

Part writes and source assembly run outside the global metadata lock. Final write
transactions recheck state, retry identity and object existence, serializing reference
acceptance with GC. `POST /metadata` updates the owner's title independently while
uploading, processing or ready. Removed/expired/failed videos cannot be restored.

## Scheduling and publication

Each claim obtains a UUID lease token, bounded expiry and attempt count in a SQLite
write transaction. Dependency completion gates claims; separate workers can encode
renditions concurrently. Retries use persisted due times. Stale workers produce only
attempt-specific outputs and cannot complete another owner's lease. Unrecoverable
inspection failure cancels dependencies. There is no arbitrary command supplied by
an HTTP caller: the FFmpeg command templates and rendition profiles are fixed.

`Pipeline` validates profiles, optional thumbnail and optional GOP preprocessing,
then generates an immutable DAG of built-in tasks. This is not an arbitrary plugin
or command engine. Finalization persists the plan, and an explicit retry cannot
replace it. Restarted workers consume persisted dependencies/profiles, irrespective
of their startup defaults. Plan generation, queue invariants, portable SQL schema
and operation ownership live in `pipeline.py`, `scheduling.py`, `schema.py` and
`lifecycle.py`, preserving the service's public methods.

Scheduling time is captured after transaction acquisition; expiry is computed just
before claiming. The first claim fixes each job's durable attempt limit, preventing
another worker's policy from prematurely failing it. Pre-upgrade leases without a
recorded policy use the former default of three. Priority pages skip blocked tasks
without loading the whole queue. Capability filtering and free local slots route
work; each Worker bounds submitted executions to its configured capacity (1..32).
This is pull scheduling, not globally optimal/GPU placement.

Heartbeats renew the lease throughout downloads, media processing and uploads.
Lease loss is detected on the next heartbeat (min(30 seconds, lease/3)); owned
FFmpeg children are killed/joined and stale results cannot commit. Completion
rechecks the lease after slow object checks. Failure uses persisted attempt counts,
and owner status includes bounded failure details. Stop prevents new work and
drains accepted operations; remote calls still obey their backend timeouts.

Publication creates a master playlist only after all rendition/thumbnail jobs have
completed and their objects exist. A durable completion row carries the full asset
map; a separate handler changes metadata to READY in a transaction. Failure between
outbox creation and handling is recoverable after restart. Takedown increments state
and blocks grants immediately; running workers must not restore a removed video.

Output names, object sets and publication dependencies are validated. A bounded
outbox batch checks objects outside the global metadata lock, then rechecks current
state before commit. DONE job references protect these objects against GC. Some
final reference checks and conservative GC still hold the metadata transaction;
slow storage can delay writers and lease renewal. No production throughput follows
from these local concurrency mechanisms.

## Media and playback

FFmpeg produces H.264/AAC HLS with aligned two-second GOP/segments, VOD playlists,
fixed rendition dimensions (aspect preserved through padding) and a PNG thumbnail.
AES-128 encrypts media segments using a per-video random key. HLS playlists reference
an authorized key route; the server propagates the playback grant into nested
playlist, segment and key URLs. Key delivery is private/no-store. This is access
control and encrypted transport assets, not FairPlay/Widevine DRM.

Optional preprocessing stream-copies the original into durable GOP-aligned Matroska
chunks. Two seconds is a target; source keyframes can produce longer chunks.
Downstream workers materialize the chunks into a local concat input, allowing
restart/retry even if the original becomes unavailable. Encoding parallelism is
per rendition, not distributed per-GOP encoding/merge. Inspection decodes all
video/audio frames. Media timeouts/cancellation kill and join the child. Diagnostics
use temporary disk and retain only a bounded tail in memory.

The edge reference uses a bounded LRU for small objects. Authorization and current
video state are checked before cache reads. Large assets stream in bounded chunks;
single byte ranges return 206/416 as appropriate. HTTPS must terminate in front of
the loopback demo for remote deployment. Signing/encryption keys reside in local
trusted persistence; production secret management/KMS is an adapter/deployment task.

Immutable published asset maps use a 128-video metadata LRU; every media request
still checks current READY state in the database. Fixed stripes coalesce cold byte
loads outside the global lock, and cleanup fences old fills. Remote byte-cache hits
still check origin existence. Download verification detects changed cached files
and merges concurrent downloads; signature caching assumes trusted local storage,
not privileged metadata-preserving tampering.

HTTP validates framing, duplicate headers/parameters, JSON and signed claims. A
bounded handler pool and absolute socket-I/O deadline cover slow headers, bodies
and writes. `HEAD /media` and byte ranges are supported; raw manifests are capped
at 1 MiB. Server close drains handlers/timers before service close drains accepted
operations. SQLite/object operations cannot be forcibly stopped by socket deadlines;
accepted mutations may commit after the client disconnects.

## Operating boundaries and validation

Leases exceed each media command's hard timeout and renew across the whole attempt;
crashed processes recover after expiry. Resource allocation is bounded worker/codec
concurrency, not a GPU cluster manager. Upload parts remain byte chunks; optional
server preprocessing creates independently playable GOP inputs.
Object/metadata writes are ordered rather than distributed transactions; unreferenced
files can be swept conservatively. Original sources are retained for re-encoding.

Expiry metadata commits before irreversible deletion, so sweep failure cannot
restore an upload whose part objects were deleted. READY/FAILED GOP intermediates
become collectible after the grace period; originals and playback assets remain
referenced. Reusing a local object refreshes its grace without replacing an open
reader's inode. Grace is not a durable object pin: paused uncommitted workers may
need to retry if their orphan outputs are swept. RUNNING attempt folders remain
protected until lease recovery changes their state. Cleanup also invalidates caches.

Tests distinguish deterministic fake-media fault scenarios from actual FFmpeg/HLS
playback. Capacity reports describe fixture duration, renditions, latency and process
layout. The book's five-million-user/CDN-cost examples are source assumptions, not
current provider pricing or a capacity claim for this implementation.

Media configuration follows the [official FFmpeg HLS documentation](https://ffmpeg.org/ffmpeg-formats.html#hls-2).

## Cross-host storage and metadata HA extension

Use a private S3-compatible bucket for authoritative objects. Each host has its own
staging/work directory. Object references retain the local adapter's content-addressed
keys; transfers verify SHA-256 and replace temporary downloads atomically. Remote
existence is checked before publication, even if a worker already has a cached copy.
Garbage collection lists only this deployment's prefix, retains metadata references
and applies a grace period. Bucket versioning/backups remain an operator responsibility.

PostgreSQL is an optional metadata backend. Initially, a database-wide transaction
advisory lock serializes mutations, preserving the existing state-machine invariants
across hosts; this is a deliberate correctness-first throughput limit. Reads are
autocommit. A multi-host libpq DSN uses target_session_attrs=read-write to discover
the primary; failures discard the connection. The next operation reconnects, while
the failed transaction is reported rather than replayed. An interrupted COMMIT is
ambiguous and must be reconciled using persisted state and idempotent operations.

Deployment uses three Patroni/PostgreSQL instances, three etcd members and strict
synchronous replication. Quorum loss or lack of synchronous replicas trades write
availability for durability. Automatic promotion needs a healthy quorum and an
eligible standby. The compose lab uses one machine; distribute failure domains for
a real multi-host deployment. SQLite data migration is offline, requires an empty
PostgreSQL target, and retains signing credentials and leases.

Reference: [Psycopg transactions](https://www.psycopg.org/psycopg3/docs/basic/transactions.html),
[Patroni replication modes](https://patroni.readthedocs.io/en/latest/replication_modes.html),
[Boto3 transfers](https://docs.aws.amazon.com/boto3/latest/guide/s3-uploading-files.html).

Current executed tests and limits: [chapter audit](chapter15-review.md).
