# S3-like Object Storage — Chapter 25

Standard-library Python reference: one catalog/API coordinator, two metadata shards and three data nodes, all independent HTTP processes. Includes private versioned buckets, delete markers, SHA256 verification, resumable multipart uploads, packed small chunks, stable listing tokens, retention, repair and garbage collection.

Python 3.10+; no extra packages/services. This is not AWS S3 wire compatibility or a production durability SLA.

## Run

From this directory:

```powershell
$env:PYTHONPATH = 'src'
python examples/network_demo.py
python -m unittest discover -s tests -v
python tools/benchmark.py --output benchmarks/results.json
```

The lab picks localhost ports, creates private tokens and stops only its six owned processes. Ignored `.runtime/lab-*` retains data/logs. If system temp access is restricted, create `.runtime/tmp` and set `TEMP`/`TMP` to that absolute path. See [validation report](benchmarks/results.md).

## Manual startup

```powershell
$env:PYTHONPATH = 'src'
python tools/init_config.py
# Each in a separate terminal with the same PYTHONPATH:
python -m object_storage.cli data --config .runtime/secrets.json --index 0 --port 9853
python -m object_storage.cli data --config .runtime/secrets.json --index 1 --port 9854
python -m object_storage.cli data --config .runtime/secrets.json --index 2 --port 9855
python -m object_storage.cli metadata --config .runtime/secrets.json --index 0 --port 9851
python -m object_storage.cli metadata --config .runtime/secrets.json --index 1 --port 9852
python -m object_storage.cli api --config .runtime/secrets.json --port 9850
```

Configuration generation refuses overwrite. Credentials include alice/bob, separate admin/internal tokens and a continuation signing secret. `write_quorum=2` may be raised to 3. Keep runtime/config private. Services bind loopback; one coordinator and fixed routing are required.

## Objects

Use an owner's bearer token:

| Route | Behavior |
| --- | --- |
| `PUT/GET/DELETE /v1/buckets/{bucket}` | Create/details/delete empty private bucket; versioning always enabled |
| `PUT /v1/buckets/{bucket}/object?key={key}` | Raw bytes, max 4 MiB; required `Idempotency-Key`, `X-SHA256` |
| `GET /v1/buckets/{bucket}/object?key={key}&version={optional}` | Raw bytes; headers include `X-Version-ID`, `X-SHA256` |
| `DELETE /v1/buckets/{bucket}/object?key={key}` | Add marker; requires `Idempotency-Key` |
| `DELETE /v1/buckets/{bucket}/object?key={key}&version={id}` | Permanently remove exact version/marker |
| `GET /v1/buckets/{bucket}/objects` | Sorted listing: prefix/versions/limit/token |
| `POST /v1/buckets/{bucket}/retention` | `{keep:N}` retains newest N live versions per key |

```python
import json
from pathlib import Path
from urllib.parse import urlencode
from object_storage.common import request, sha
c = json.loads(Path('.runtime/secrets.json').read_text())
url, token = 'http://127.0.0.1:9850', c['users']['alice']
request(url + '/v1/buckets/demo-bucket', token, b'', 'PUT')
path = '/v1/buckets/demo-bucket/object?' + urlencode({'key': 'folder/hello.txt'})
data = b'hello object'
version = request(url + path, token, data, 'PUT', headers={
    'Idempotency-Key': 'put-001', 'X-SHA256': sha(data)})
assert request(url + path, token, raw=True) == data
```

Keys are flat UTF-8 names up to 512 bytes and may contain slashes; URL-encode query values. Bucket names are 3–63 lowercase letters/digits/hyphens. Matching key/content retries return the same version; changed content returns 409. An old receipt cannot recreate a removed version. Markers hide latest objects while preserving older reads; removing a marker reveals the prior version. Delete buckets only after removing all live versions/markers and active/completing sessions.

## Multipart uploads

| Route | Meaning |
| --- | --- |
| `POST /v1/buckets/{bucket}/uploads` | `{key,request_key,sha256,size}`; one-hour active lifetime |
| `GET /v1/uploads/{id}` | Durable state and accepted part manifests |
| `PUT /v1/uploads/{id}/parts/{number}` | Raw 1 byte–1 MiB; required `X-SHA256`; numbers 1–64 |
| `POST /v1/uploads/{id}/complete` | Verify contiguous parts/whole size/hash and publish |
| `DELETE /v1/uploads/{id}` | Abort active session; frozen completion cannot abort |

```python
data = b'A' * 700000 + b'B' * 500000
upload = request(url + '/v1/buckets/demo-bucket/uploads', token, {
    'key': 'large', 'request_key': 'multipart-001', 'sha256': sha(data), 'size': len(data)})['id']
for number, offset in enumerate(range(0, len(data), 700000), 1):
    part = data[offset:offset + 700000]
    request(url + f'/v1/uploads/{upload}/parts/{number}', token, part, 'PUT',
            headers={'X-SHA256': sha(part)})
request(url + f'/v1/uploads/{upload}/complete', token, {}, 'POST')
```

Maximum is 64 MiB/256 chunks; chunks are at most 256 KiB. A part number cannot change bytes. Resume missing parts from persisted session state after API restart. Parts are invisible until completion. A durable completing intent precedes publication; retry or maintenance resumes interrupted completion idempotently. Expiry aborts only active sessions.

## Listing and maintenance

Listing freezes at most 10,000 rows across metadata shards. Page size is 1–1000. Use returned `continuation` as `token` with identical prefix, versions mode and limit; tokens bind user/bucket/query and expire in five minutes. Adds/deletion markers do not shift later pages. Frozen rows do not pin permanently deleted content.

Nonempty chunks are attempted on three nodes and need at least two acknowledgements by default before metadata publication. Reads bypass damaged/unavailable copies and verify whole-object hash. Fewer than the write threshold means 503 and no published object. The nodes are local processes, not independent physical failure domains.

```python
report = request(url + '/admin/maintenance', c['admin_token'], {'grace': 60})
print(report)  # recovery, repaired_copies, copy_failures, unrecoverable and GC
```

Maintenance resumes completing uploads, expires active ones, gathers all version/upload references, repairs replicas, and collects old unreferenced chunks. Missing metadata halts GC. Recently removed versions are protected through grace; 50,000 live unique chunks is the maintenance budget. Tests explicitly use zero grace; normal default is 60 seconds.

Data-node `/status`, `/chunks/{hash}`, POST `/collect` and POST `/compact` require internal credentials. Small chunks (at most 64 KiB) share a checksummed append file with delete tombstones and recoverable offset index; large chunks use separate files. Compaction switches CURRENT atomically after writing a new live generation. Run it after repair/GC. Never invoke node collection with an incomplete reference set outside the coordinator.

Catalog and metadata shards have no replicas or election. One coordinator permits shared ordinary operations and exclusively gates GC, bucket deletion and listing capture. Downloads buffer bounded content, not GB streams. No AWS signatures, IAM, TLS, encryption, erasure coding, automatic multi-host repair or online resharding exists. Receipts/removed metadata lack automatic archival. Process/file fault tests do not establish 100 PB, six-nine durability or four-nine availability. Details: [design](docs/design.md).

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
speedup comparison or a production SLA. See [chapter review](docs/chapter25-review.md)
and [measurements](benchmarks/results.md).

Remaining chapter-scale work: placement heartbeats/consensus, independent AZs,
metadata/catalog HA, multi-pack rotation/per-core append streams, streaming GB
objects, erasure coding, automatic repair scheduling, online resharding, cloud
IAM/TLS/encryption and lifecycle/backup/100PB durability validation. No real AWS
service was used, and no commit or push was made.

Bucket deletion also rejects pending publication intents with409, preventing
recovery from publishing an inaccessible version into an already removed bucket.
