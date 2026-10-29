# Requirements: Video Streaming System

Source: root System Design Interview.md, Chapter 15, Design YouTube.

- R1: Upload and watch small/medium videos, with a configurable maximum of 1 GB.
  Metadata and video bytes follow separate paths. No recommendations/comments/live streaming.
- R2: Issue authenticated, expiring, part-specific upload grants. Accept resumable
  parts, verify lengths/checksums and reject conflicting retries or another owner.
- R3: Persist upload state, source objects, job dependencies, leases and retries.
  Finalization requires every part and the declared total size; optional full SHA256
  validates the assembled source. Metadata must not become ready before all assets exist.
- R4: Execute an offline DAG: inspect -> parallel renditions/thumbnail -> publish.
  Workers claim jobs atomically; stale lease owners cannot complete a reclaimed job.
  Recoverable failures retry with bounded backoff, malformed media fails immediately,
  exhausted jobs enter a durable failed state and cancel dependent work.
- R5: Supply a real FFmpeg adapter, playable multi-resolution HLS, aligned keyframes,
  thumbnail output, and AES-128 encrypted segments with authorized key retrieval.
  Test-only media adapters must never be described as actual transcoding.
- R6: Persist publication in a completion outbox; a handler atomically updates ready
  metadata after all outputs are stored. Crash replay cannot publish twice or lose completion.
- R7: Serve bounded HTTP metadata/upload/playback operations, HLS manifests/segments,
  byte ranges and expiring playback grants. Takedown must override cached delivery.
- R8: Use pluggable object/media interfaces and a bounded read cache for a local CDN
  reference. Cleanup removes abandoned parts/attempt files without deleting active assets.
- R9: Run actual FFmpeg and network/process tests for interrupted uploads, worker lease
  recovery, retries, corrupt media, encrypted playback and completion recovery.
- R10: Publish examples, measured media processing results and English/Chinese READMEs.
  SQLite/local files are reference persistence, not sharded/replicated cloud storage.

- R11: Support an S3-compatible private bucket shared by API and workers with
  independent local directories. Verify SHA-256 on downloads, never treat local
  cache presence as remote durability, and retain referenced objects during GC.
- R12: Support PostgreSQL primary discovery through a multi-host DSN, transactional
  claims and lease fencing across connections, and reconnect after primary loss.
  Do not replay an uncertain write/commit automatically. Surface temporary database
  failure as 503; workers resume polling without silently losing claimed work.
- R13: Provide a reproducible synchronous Patroni/PostgreSQL HA deployment with
  quorum coordination and object storage; record actual failover tests separately
  from configuration validation. Expose migration of SQLite metadata explicitly.

Cloud CDN deployment, multi-region upload
centers, DRM integrations, adaptive-player UI, watermarking, GPU resource scheduling
and content moderation classifiers remain extension interfaces/explicit follow-up tasks.
