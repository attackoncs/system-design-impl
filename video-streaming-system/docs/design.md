# Design: Video Streaming System

## Data flow

```text
client -> authenticated API -> expiring part grants -> original object storage
                               |                            |
                         video metadata               durable DAG jobs
                                                            |
                                  inspect -> rendition(s) + thumbnail -> publish
                                                            |
                                                  completion outbox
                                                            |
                                    handler -> READY metadata + encoded object map
                                                            |
                          playback grant -> bounded edge cache -> HLS/segments/key
```

The original chapter's upload, metadata, transcode and completion paths are separate.
SQLite stores credentials, grants' signing secret, upload parts, video state, jobs,
leases and completion outbox. Content-addressed files are written with atomic rename
and fsync before metadata references them. API/worker processes share local storage;
real multi-host deployments need a replicated database and object storage adapters.

## Upload and authorization

Owner credentials are provisioned out of band. Upload requests declare total bytes
and fixed part size; an HMAC grant binds each part's video, index, expected length
and expiration. Full parts can be retried idempotently; conflicting bytes fail.
Finalization streams parts into a source object and checks total/full SHA256 before
atomically scheduling the DAG. An interrupted finalization can leave an unreferenced
object, but cannot expose a ready video. Owners cannot finalize other owners' uploads.

## Scheduling and publication

Each claim obtains a UUID lease token, bounded expiry and attempt count in a SQLite
write transaction. Dependency completion gates claims; separate workers can encode
renditions concurrently. Retries use persisted due times. Stale workers produce only
attempt-specific outputs and cannot complete another owner's lease. Unrecoverable
inspection failure cancels dependencies. There is no arbitrary command supplied by
an HTTP caller: the FFmpeg command templates and rendition profiles are fixed.

Publication creates a master playlist only after all rendition/thumbnail jobs have
completed and their objects exist. A durable completion row carries the full asset
map; a separate handler changes metadata to READY in a transaction. Failure between
outbox creation and handling is recoverable after restart. Takedown increments state
and blocks grants immediately; running workers must not restore a removed video.

## Media and playback

FFmpeg produces H.264/AAC HLS with aligned two-second GOP/segments, VOD playlists,
fixed rendition dimensions (aspect preserved through padding) and a PNG thumbnail.
AES-128 encrypts media segments using a per-video random key. HLS playlists reference
an authorized key route; the server propagates the playback grant into nested
playlist, segment and key URLs. Key delivery is private/no-store. This is access
control and encrypted transport assets, not FairPlay/Widevine DRM.

The edge reference uses a bounded LRU for small objects. Authorization and current
video state are checked before cache reads. Large assets stream in bounded chunks;
single byte ranges return 206/416 as appropriate. HTTPS must terminate in front of
the loopback demo for remote deployment. Signing/encryption keys reside in local
trusted persistence; production secret management/KMS is an adapter/deployment task.

## Operating boundaries and validation

Leases must exceed each task's hard media timeout; processes that crash are recovered
after expiry. Resource allocation is a bounded worker/codec concurrency, not a GPU
cluster manager. Upload parts are byte chunks; FFmpeg handles GOP-aligned output on
the server, rather than pretending arbitrary upload chunks are independently playable.
Object/metadata writes are ordered rather than distributed transactions; unreferenced
files can be swept conservatively. Original sources are retained for re-encoding.

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
