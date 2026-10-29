# Requirements: Distributed Email Service

Based on Chapter 24 of the root-level《System Design Interview.md》. The reference implementation covers HTTP mailboxes, send/receive, read filtering, subject/sender/body search, attachments, and a replaceable scanning boundary. Authentication plugs in via a static-token adapter; a full account system is not implemented.

1. Mailboxes are sharded by user; primary metadata and the inverted index are persisted; independent read-only nodes replicate asynchronously over HTTP. Replicas may serve stale reads during failures but must never automatically accept writes.
2. HTTP sending uses a durable idempotency key and a queue; an independent worker transfers messages via the real local SMTP protocol to configured `local.test` mailboxes. No MX lookups and no sending to external addresses.
3. The receive gateway persists the raw message reference and envelope before successful acknowledgement; retransmission of the same delivery ID after a lost acknowledgement does not create a duplicate inbox copy. Multi-recipient partial failures can be replayed, and deletion tombstones prevent mail from being resurrected.
4. Body, raw RFC message and attachments are stored in an independent HTTP object service with SHA256 integrity verification; attachment downloads must first verify the owning user and the message state.
5. At most 3 attachments of 2 MiB each, at most 8 MiB for the complete RFC message, and a 65536-character body; metadata databases must not store attachment binaries.
6. Mailbox/search pagination of at most 50 items; supports read filtering and exact-term AND search over subject/sender/body; marking read/delete uses version-conditioned updates to prevent lost writes.
7. The scanning interface is replaceable; when no real scanner is configured, messages are marked unscanned and quarantined, and test marker rules must not claim real anti-virus/anti-spam capability. Scanner unavailability keeps a durable retry queue, and quarantined attachments are forbidden from download.
8. Verify real network send/receive, duplicate acknowledgements, node recovery, metadata replication, read-state conflicts, search-deletion synchronization, attachment corruption and authorization boundaries, and provide a measured test report.

Not included: public email delivery, MX/DNS, IMAP/POP, real AV/anti-spam, full OAuth identity, HTML rich text, secure attachment preview, object replication, automatic metadata primary/replica failover, or production billion-user capacity. All mail and recipients are local synthetic test data.

2026-10-26 review acceptance: fixed-folder counts/listing, user-scoped durable
long polling with lineage cursor, optional read barrier, watermark-bound bounded
metadata cache, strict replicated replay, fenced sender/receiver claims and
bounded direct HTTP/SMTP ownership. Validate concurrency, resource cleanup and
real network behavior; production AV/HA/scale remain explicit gaps.
