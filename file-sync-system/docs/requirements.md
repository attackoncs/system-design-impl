# Requirements: File Sync System

Source: root System Design Interview.md, Chapter 16, Design Google Drive.

- R1. Upload/download arbitrary binary files up to 10 GiB, including empty files.
  Split files into bounded blocks (default 4 MiB); do not buffer an entire large file.
- R2. Authenticated, resumable upload sessions with request IDs, SHA-256 checksums,
  account/workspace-scoped deduplication and atomic publication after every block
  is stored and verified. Conflicting retries must fail.
- R3. Compress blocks and encrypt at rest using cryptography AES-GCM, random nonces
  and authenticated namespace/hash/size metadata. Detect ciphertext corruption.
- R4. Immutable revisions, optimistic base-revision writes, conflict copies preserving
  concurrent edits, tombstones, revision listing and restoration.
- R5. Owner-controlled workspace read/write sharing, immediate server-side revocation,
  quota enforcement including retained revisions and pending uploads.
- R6. Transactional per-user change events, long polling, persistent device cursors,
  explicit acknowledgements and replay after disconnection/server restart.
- R7. A real filesystem client uses safe relative paths, atomic file replacement,
  persisted local state and block cache; transfer only missing/modified blocks,
  retain unsynchronized local edits rather than overwrite them.
- R8. Reference counting, revision retention, conservative orphan/session cleanup,
  explicit cold-tier archive/restore behavior without claiming actual Glacier integration.
- R9. Repeatable correctness/fault tests plus independent network/process demos.
  Measure synchronization latency, block reuse and bytes transferred.

Reference persistence is local SQLite WAL and encrypted filesystem objects. No
claim of metadata HA, cross-region replication, ten-million-user scale, commercial
mobile clients, document co-editing or production TLS/KMS. Those are deployment
extensions, not silently simulated features.
