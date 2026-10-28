# Distributed email reference design

## Processes and data ownership

The local lab has nine independent processes: one HTTP API router, two user-shard primary HTTP servers, two read-replica HTTP servers, one object HTTP server, one SMTP gateway (with its control HTTP endpoint and receive-processing thread), and two send workers. The API, replicas and workers never open primary databases; they use HTTP. The SMTP gateway talks actual TCP SMTP to the send workers and HTTP to objects/mailbox primaries. Only corruption tests directly alter an owned object file.

User placement is `first_64_bits(SHA256(canonical_JSON(user))) % shard_count`. Each primary owns an independent SQLite WAL database. Persisted shard/count/role metadata rejects inconsistent startup. The lab deliberately chooses a sender on shard 0 and a recipient on shard 1; the default human-readable account names are not assumed to hash evenly. Configuration defines static placement and one replica per primary. Online resharding and automated promotion are not implemented.

Metadata includes owner, immutable descriptor, body/object keys, attachment descriptors, folder, read flag, version, deletion tombstone and a stable mailbox creation sequence. Raw bytes remain in the object service. A primary also owns upload ownership references, durable send receipts, receive-dedup receipts and the outbound queue. These primary-only control tables are not promoted from a replica.

## HTTP send, object staging and durable queue

Clients have configured user bearer credentials. Uploading at most a 2 MiB attachment creates an immutable SHA256-addressed object, then a user-owned upload reference in the user's primary. A send request accepts 1–10 configured local recipients, a subject, plain-text body, up to three attachment upload IDs and a stable request key. A caller cannot attach another user's upload.

The send service validates the request and stages body/RFC MIME objects before the metadata transaction. A stable `X-Delivery-ID`, Message-ID and MIME boundary derive from sender/request key; MIME bytes are reused from the saved raw object for every queue retry. Header-injection subject newlines are rejected. SMTP uses a local subset of RFC/MIME processing rather than a complete public MTA. The total RFC message limit of 8 MiB can be stricter than the sum of individual attachment limits after base64 expansion.

Sent-folder metadata, postings, an outbound job and `(user,request_key)` receipt commit in one SQLite `BEGIN IMMEDIATE` transaction. Same key/content returns the original sent ID; changed content fails 409. Objects staged by failed or concurrent duplicate submissions may be unreferenced; there is no unsafe immediate deletion because objects are content-addressed and may be shared. An object GC and privacy/receipt retention design are production work.

Each send job has a persistent attempt count, due time, two-second lease and random claim token. Network work occurs outside the metadata write transaction. Success updates the sent copy to `smtp-accepted`; this is gateway acceptance, **not** proof that every recipient has received the message. Failure persists an error and exponential retry delay (capped at 6.4 seconds by the current exponent cap). A crashed worker's lease can be reclaimed; a stale token cannot settle a newer claim. No queue receipt is acknowledged before the corresponding local commit.

## Local SMTP and receiving

Send workers connect only to `127.0.0.1` on the configured SMTP port. They use Python `smtplib`, AUTH PLAIN with a separate internal SMTP credential, MAIL/RCPT/DATA and durable saved MIME. The local gateway implements EHLO, AUTH PLAIN, MAIL, RCPT, DATA, RSET and QUIT, dot unstuffing, a message-size bound and command-order checks. It is intentionally not a production SMTP server: no TLS, public MX, DNS, SMTPUTF8, DKIM, SPF, DMARC or public relay exists. Both sender and recipients must be configured `local.test` accounts. CLI servers bind loopback only.

The gateway puts raw data into the object service and persists an immutable delivery ID, raw key, envelope and fingerprint before replying 250. The test control can reply 451 after commit, emulating a missing acknowledgement. The sender retries identical bytes; the gateway accepts a matching duplicate without creating another spool item. Conflicting content under that ID fails. Deduplication uses the authenticated local delivery ID, not arbitrary internet Message-ID headers.

One gateway-owned receive thread processes its durable spool. It parses bounded MIME parts, retrieves/validates objects, extracts plain text and attachments, applies the configured scanning policy, and calls each recipient's primary via HTTP. Inbox ID is deterministic for `(recipient,delivery_id)` and a durable receive receipt protects partial multi-recipient replay. A deleted inbox copy retains its receipt and tombstone, so delivery retries cannot resurrect it. The gateway marks a spool item done only after all recipient writes succeed. Malformed MIME/unsupported encodings produce an inspectable rejected spool row; transient scan/storage/mailbox failures remain pending. A gateway restart resumes retained rows; multiple gateway writers/receive-loop ownership or distributed spool failover are not supplied.

## Scanning and quarantine boundaries

`Scanner` is an explicit replaceable interface. The unconfigured policy returns `folder=quarantine, scan=unscanned, scan_engine=none`. This is the default from configuration generation. There is no silent claim of malware-free delivery.

For synthetic tests only, `TestMarkerScanner` searches raw and decoded payload bytes for `LOCAL_TEST_VIRUS_MARKER` or `LOCAL_TEST_SPAM_MARKER`. It reports `scan_engine=test-markers-only`; other messages report `test-policy-pass`, which **does not mean virus-free**. The first marker quarantines, the second routes to spam. It does not inspect archives or recognize real malware/spam. A test-only scanner-unavailable switch leaves messages durably queued for retry. No third-party scanner integration or accuracy claim is made. Quarantined received attachments cannot be downloaded via the user API; no automated release/rescan workflow is implemented.

## Objects and authorization

The object service stores content under hex SHA256 keys with staged writes, file flush/fsync and atomic replacement. Every GET validates the stored bytes against the key, and the HTTP client validates again. Existing corrupt objects fail instead of silently returning altered data. Objects are limited to 8 MiB, individual uploads to 2 MiB. The service requires an internal bearer token. Public users cannot fetch an arbitrary key: the API first loads a nondeleted owned message, checks quarantine policy, chooses a referenced attachment, then retrieves it internally. Path-like keys are rejected. Download filenames are metadata; they never choose object filesystem paths.

The single object service and spool are not replicated. Metadata replica availability therefore does not imply full-body/attachment availability when object storage fails. There is no encryption, user quota, byte-range streaming, object replication, archive lifecycle or shared-object garbage collector. JSON/base64 transport incurs copies and is bounded for this reference; a large production object path would stream bytes.

## Read flags, pagination and search

Mail bodies are immutable. Read-state and delete requests require an exact current version; stale updates return 409. A client reading a stale replica must refresh the primary before retrying a write. IDs are opaque, and owner filtering is part of every message/attachment lookup. Other users receive 404. Static tokens are an identity integration boundary, not a full authentication service. Admin tokens operate only the simulator controls; they do not silently impersonate mailbox owners.

Mailbox order uses a stable per-shard creation sequence. `before` pagination returns at most 50 messages and does not shift older pages when new mail arrives. A cursor is not a global snapshot of concurrent edits. Supported folders are inbox, sent, spam and quarantine; read filtering is exact.

The inverted index has `(mail,field,term)` entries with field/term lookup indexes. Unicode `\w+` casefolded tokenization is simple, not stemming, substring or language-aware segmentation. Search accepts at most five terms and 100 query characters; all terms must appear in the chosen field (or anywhere across fields for `all`). A mail is limited to 4096 unique field-term pairs. Subject, envelope sender and plain-text body are indexed. Attachment contents and HTML rendering/search are outside scope. Primary metadata/index changes share one transaction, including index removal on delete; there is no primary index-update lag window. Replicas eventually apply the same terms and tombstones, so replica search can lag.

## Metadata replication and failover semantics

Each primary mutation appends a checksum-protected metadata/index change in the same transaction as the mail write. Replica HTTP pulls request changes after a durable sequence. Pages have at most 100 entries and a roughly 2 MiB payload budget (one entry may use the whole budget). Replica checks shard assignment, source lineage, contiguous sequence and checksum; it applies mail rows, terms and its checkpoint atomically. Duplicates are ignored. Gaps, changed lineage or corruption stop progress and expose errors. There is no log trimming; a new replica can replay from zero.

Reads expose `lineage`, `position` and the chosen primary/replica source. The router prefers primary reads and falls back to the configured replica only on availability errors, or clients may request `replica=true`. Authoritative 404/409 responses are not masked by an older replica. A replica can serve its last durable state while the primary is unavailable, even while its poll status shows an error. This explicitly permits stale mail, read flags and deletion visibility. There is no read-your-writes barrier or linearizable read claim. Writes never fail over to the replica, and there is no election/split-brain promotion.

Only mailbox metadata and postings replicate; send queues, upload ownership and dedup control tables stay on primaries. Thus the replicas cannot safely replace a lost primary for write service. Process-restart tests retain all local disks. Production reliability requires replicated primary storage/control tables, replicated objects/spool, an election/fencing protocol, privacy lifecycle and multi-host validation. The original billion-user/100k-send-QPS targets are not proven by this local lab.

## Chapter 24 engineering review (2026-10-26)

The review adds `GET /v1/folders` (four fixed folders with total/unread counts),
`GET /v1/folders/{folder}/messages`, and `GET /v1/changes?after=0&wait=2`.
Changes contain only the authenticated user's message ID, version, read/deleted
flags and folder. Resume with both `after` and the returned `lineage`; pages
contain at most 100 events, `more` indicates another page, and `position` is the
next cursor (it can also advance past other users' shard events). `head` is the
snapshot's shard watermark. Long polling waits at most two seconds. This is a
durable HTTP notification fallback, not WebSocket/JMAP. A missing historical log
on an older replica returns 409: refresh the mailbox and resume from its current
watermark. No body or attachment content is included in notification events.

GET requests accept `min_position=N&lineage=L`. A lagging node returns 503;
replacement lineage returns 409. Write receipts include their transaction's
watermark. Without a barrier, replica fallback can still be stale. Folder and
listing metadata use a process-local LRU bounded to 128 entries/4 MiB, keyed by
user, filters and exact lineage/position. Returned values are isolated copies;
new commits invalidate reuse. This is not a distributed Redis cache.

The code separates record validation, replica application, receiving spool,
route contracts, direct transport and bounded server ownership. Sender claims
use 30-second leases with a shared 20-second I/O budget; receive claims use
90-second leases with a shared 60-second budget. Settlement checks token and
expiry after the writer lock. Metadata/index/log/checkpoint update atomically;
replicas validate replays and retain the full change log. SMTP AUTH requires
`local-worker` plus the private SMTP token. SMTP and HTTP each admit at most
32 concurrent connections/requests. Shutdown cancels socket I/O and drains owned
threads; socket deadlines do not forcibly terminate arbitrary SQL/CPU/DNS work.

MIME validates decoded defects, duplicate fields, charset and decoded budgets;
nested message/rfc822 or multipart-container attachments are explicitly rejected
(use octet-stream). Object reads are bounded, receipts verify SHA256/length, and
64 fixed publication lock stripes protect duplicate uploads on Windows. One
object-service process must own its data directory.

Final validation: **96 tests passed, 30.741 seconds**, `-W error`, with actual
localhost HTTP/SMTP and nine processes. The new notification demo verified one
inbox event, versions 2/3 after resume, replica read barrier and zero outsider
events. See [review evidence](chapter24-review.md) and
[measurements](../benchmarks/results.md). No commits or pushes were made.

Remaining chapter-scale work includes real AV/spam and deliverability feedback,
Cc/Bcc and conversation threading, complete folder management, public SMTP/MX,
TLS/OAuth/DKIM/SPF/DMARC, placement/consensus and write-primary failover, replicated
objects/spool, backup/retention and multi-host/billion-user validation. This
reference does not claim all production requirements are implemented.
