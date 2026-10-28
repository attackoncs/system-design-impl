# Distributed Email Service — Chapter 24

A standard-library Python reference with **nine real processes**: HTTP API, two mailbox primaries, two asynchronous read replicas, an immutable object service, a local SMTP gateway and two send workers. Supports HTTP mailboxes, attachments, read/unread filters, subject/sender/body search, durable send/receive deduplication, quarantine and retained-disk recovery.

Python 3.10+. No extra packages or external services. SMTP is confined to configured `local.test` mailboxes on loopback; there is no internet delivery or MX lookup. Test scanner rules are explicitly **not real anti-virus or anti-spam**.

## Run

From this directory:

```powershell
$env:PYTHONPATH = 'src'
python examples/network_demo.py
python -m unittest discover -s tests -v
python tools/benchmark.py --output benchmarks/results.json
```

The lab picks local ports, creates private tokens, starts nine independent processes and stops only those it owns. Data/logs stay under ignored `.runtime/lab-*`. If system temp access is restricted, create `.runtime/tmp` and set `TEMP` and `TMP` to that absolute directory. See [validation report](benchmarks/results.md).

## Manual startup

```powershell
$env:PYTHONPATH = 'src'
python tools/init_config.py --test-scanner
# Each command runs in its own terminal with the same PYTHONPATH:
python -m distributed_email.cli objects --config .runtime/secrets.json --port 9841
python -m distributed_email.cli primary --config .runtime/secrets.json --shard 0 --port 9842
python -m distributed_email.cli primary --config .runtime/secrets.json --shard 1 --port 9843
python -m distributed_email.cli replica --config .runtime/secrets.json --shard 0 --port 9844
python -m distributed_email.cli replica --config .runtime/secrets.json --shard 1 --port 9845
python -m distributed_email.cli smtp --config .runtime/secrets.json --port 9846
python -m distributed_email.cli api --config .runtime/secrets.json --port 9840
python -m distributed_email.cli worker --config .runtime/secrets.json --shard 0
python -m distributed_email.cli worker --config .runtime/secrets.json --shard 1
```

SMTP wire port is 9847; 9846 is its authenticated administration HTTP port. Config generation refuses overwrite. It creates distinct mailbox credentials (`users`), `internal_token`, `smtp_token`, `admin_token`, ordered primary/replica URLs and the object URL. Keep credentials and runtime files private. All servers require loopback binding. Omit `--test-scanner` to use the safe unconfigured policy: all received mail goes to quarantine with `scan=unscanned`.

## HTTP client

Use the mailbox owner's bearer token. Public URLs below are on the API process:

| Route | Behavior |
| --- | --- |
| `POST /v1/uploads` | `{filename,data}` where data is base64; returns an owner-scoped `upload_id` |
| `POST /v1/messages` | `{request_key,to,subject,body,attachment_ids}`; durable sent copy + outbound job |
| `GET /v1/messages` | List up to 50; filters `folder`, `read`, `before`, `q`, `field`; optional `replica=true` |
| `GET /v1/messages/{id}` | Owned message descriptor, plain-text body, version and metadata position |
| `PUT /v1/messages/{id}` | `{version,read}` compare-and-swap read flag |
| `DELETE /v1/messages/{id}?version=N` | Tombstone with version check; removes search entries |
| `GET /v1/messages/{id}/attachments/{index}` | Owner-authorized base64 download with SHA256 verification; zero-based index |
| `GET /health` | Minimal public process status |

```python
import base64, json
from pathlib import Path
from distributed_email.common import http_json
c = json.loads(Path('.runtime/secrets.json').read_text())
api = 'http://127.0.0.1:9840'
alice = c['users']['alice']
upload = http_json(api + '/v1/uploads', alice,
                   {'filename': 'hello.txt', 'data': base64.b64encode(b'attachment').decode()})
sent = http_json(api + '/v1/messages', alice, {
    'request_key': 'mail-001', 'to': ['bob@local.test'], 'subject': 'Project update',
    'body': 'Searchable project notes', 'attachment_ids': [upload['upload_id']]})
print(sent)
# Poll Bob's inbox; initial send acceptance is not recipient-delivery confirmation.
print(http_json(api + '/v1/messages?q=project&field=body', c['users']['bob']))
```

Different mail requires a new request key. Reusing the same key/content returns the original sent message ID; changed content returns 409. A message may have 1–10 distinct configured local recipients. Each attachment is at most 2 MiB, at most three per message, complete RFC MIME at most 8 MiB, body at most 65536 characters and subject at most 200 characters. Base64/MIME overhead counts toward the RFC limit. No external recipient is accepted.

Folders: `inbox` (default), `sent`, `spam`, `quarantine`. `read=true/false` filters read state. Search `field` is `all` (default), `subject`, `sender`, or `body`; up to five Unicode word tokens, AND semantics, casefolded, max 100 query characters. There is no substring/stemming/attachment-content search. Each message is limited to 4096 indexed field terms. `next_before` drives stable older-mail pagination. Contents are immutable; read-state updates and deletion require the current `version` and return 409 if another client changed it.

## Delivery, scanning and replicas

- A successful HTTP send means the sent copy and job are durable. `descriptor.delivery=smtp-accepted` means the local SMTP spool accepted it, not that every inbox is updated. Durable jobs retry with stable raw bytes/IDs and fenced two-second leases.
- SMTP acceptance persists the raw object reference and envelope before a 250 reply. Matching retries deduplicate in the gateway and recipient primary. Receive receipts and delete tombstones prevent a retry from restoring deleted mail. Transient scan/storage/mailbox failures keep spool jobs pending; malformed MIME remains inspectable as rejected.
- The default scanner is **unconfigured**, so mail is quarantined and attachment downloads are blocked. `--test-scanner` enables only literal `LOCAL_TEST_VIRUS_MARKER` / `LOCAL_TEST_SPAM_MARKER` rules. Results identify `scan_engine=test-markers-only`; `test-policy-pass` does not mean malware-free. No real AV provider is implemented.
- Body, raw RFC messages and attachments live in the separate SHA256 object service. Metadata contains keys and filenames, not large object bytes. Public downloads require an owned nondeleted message and quarantine checks; knowing a key alone does not grant access.
- Primary metadata and inverted-index updates commit together. Replicas pull checksummed change pages over HTTP and atomically update metadata, terms and durable position. Read responses expose source, lineage and position. Replicas can lag in mail visibility, flags, search and deletion.
- The API prefers primary reads and falls back to the configured read replica on availability failures; `replica=true` explicitly chooses it. Writes never promote or fail over. A stale replica can cause a version conflict when the client writes to primary; refresh before retrying. Sender queues/dedup control tables are not replicated for write promotion.

Primary/replica `GET /internal/status` requires the internal token and reports position/lineage, pending send jobs/errors, plus replica polling state. Primary `/internal/changes`, `/internal/receive`, `/internal/claim` and `/internal/settle` are internal-only and are not routed through the public API. Object endpoints also require the internal token.

SMTP gateway `GET /admin/status` exposes durable spool status; `POST /admin/control` accepts `{"lose_ack":true,"scanner_down":true}` under the admin token for local fault experiments. The first returns SMTP 451 after durable acceptance; the second retains messages for scanner retry. Reset both false to recover. These switches operate only the owned local test gateway.

## Limits

This is not a public mail provider. No external send adapter is enabled, and external sending requires separate authorization and implementation. No real scanner, full authentication/OAuth, TLS, DKIM/SPF/DMARC, MX lookup, HTML client, IMAP/POP or mail fraud protections are implemented. Metadata replicas are asynchronous read copies, not consensus or automatic primary replacement. Objects and SMTP spool have one owner and no replication; full-body reads need object availability. Data/receipts/change logs have no automatic retention or garbage collection. Tests retain local disks and do not prove the book's billion-user scale or availability. See [design](docs/design.md).

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
events. See [review evidence](docs/chapter24-review.md) and
[measurements](benchmarks/results.md). No commits or pushes were made.

Remaining chapter-scale work includes real AV/spam and deliverability feedback,
Cc/Bcc and conversation threading, complete folder management, public SMTP/MX,
TLS/OAuth/DKIM/SPF/DMARC, placement/consensus and write-primary failover, replicated
objects/spool, backup/retention and multi-host/billion-user validation. This
reference does not claim all production requirements are implemented.
