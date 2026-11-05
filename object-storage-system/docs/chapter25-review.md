# Chapter 25 source and error-boundary review — 2026-11-05

Read the complete local chapter (lines11925–12813), inspected actual pinned
MinIO multipart and SeaweedFS compaction/recovery source, and used CodeGraph
for local data, metadata, listing, publication and HTTP paths. See the
[source audit and chapter matrix](open-source-review_cn.md).

The existing reference covers the chapter's core bucket/read/write/version/list
paths within documented size and single-host limits. No confirmed new defect
was found in the node, manifest/quorum, catalog, metadata or listing reviews.
The confirmed HTTP defect was excessive nesting in an upstream error body:
`RecursionError` escaped from inside `except HTTPError`, so a valid client
request lost its connection without an API error response. The production fix
adds that exception to the existing neutral503 fallback. Valid upstream409
and invalid client JSON400 remain unchanged. No automatic write retry was added.

One new actual-HTTP regression uses real Service/Catalog/API with a controlled
metadata peer. Six cases cover malformed JSON, duplicate keys, NaN,
4000-level nested409/503 bodies, and valid409. Before the fix the two deep
cases raised a client connection error; after the fix they return503.
It verifies one `/publish`, retained pending intent and subsequent health.
The peer does not commit metadata, so this is not proof of a real lost-ACK
commit. Existing durable recovery tests remain separate. The existing client
JSON test also verifies deep input still returns400.

Baseline **137 tests passed in41.699s**. Final **138 passed in41.769s**, no skips,
`-W error`. HTTP suite:20 passed in2.422s. Both actual six-process examples passed:
1,000,000-byte multipart, historical-version and one-node-down reads match;
listing ownership, mixed one-item-page snapshot, concurrent completion and
stale generation409 are verified. Forty benchmark objects passed exact reads.
[Raw feature evidence](../benchmarks/features-2026-11-05.json),
[raw performance](../benchmarks/review-2026-11-05.json),
[measurement interpretation](../benchmarks/results.md).

Graph increment re-parsed3 files, updating113 nodes and1199 edges without errors;
these are update counts, not repository totals. Explicit direct two-hop impact
for `http.py` reports11 directly changed nodes,23 impacted nodes and3 additional
files, without truncation;56 unresolved call sites remain. The shared handler's
structural risk is high, which guided tests and independent source review;
the graph alone does not prove runtime correctness or full coverage.

Only one production exception tuple changed; this review preserves the existing
bounded concurrent architecture. No real AWS services, commits or pushes were
used. GB streaming, placement consensus/heartbeats, independent AZs, metadata/
catalog HA, multi-pack/per-core append streams, erasure coding, autonomous repair,
online resharding, IAM/TLS/encryption and100PB/SLA validation remain gaps.

---

# Historical Chapter 25 review — 2026-10-26

Read local book lines11925–12813. Private buckets, immutable bytes/version IDs,
delete markers, user-key metadata sharding, checksums, replicated data, packing,
multipart and GC existed. Added owner-bucket enumeration and same-level prefix
listing. Preserved recursive listing and migrated legacy signed snapshots.

Catalog/listing/metadata contracts/chunk I/O/runtime/transport are separate.
Metadata freezes manifests before lock waits, samples clocks after writer
ownership and scans bounded references. Data nodes share reads, serialize writes
and fail closed after uncertain append/index/CURRENT failures until recovery.
HTTP admission32, coordinator operations8, shared replication threads12, upload
stripes64. Ordinary operations overlap; list capture/delete/GC are exclusive.

A cross-review found an actual correctness gap: local RPC timeout did not stop
an accepted remote collect or metadata publish. Durable catalog publication
intents now cover unknown metadata results; maintenance replays them before
scanning references. The node's random generation is captured before that scan
and checked under its writer lock at sweep time. Every accepted PUT (including
duplicate content) invalidates an old sweep. Repairs can defer collection until
the next full scan. No cross-machine clock assumption is required for this
fence. Historical pre-upgrade unknown operations cannot be reconstructed: drain
old work before upgrading. APIs still require one catalog/coordinator owner;
direct metadata writers or incomplete node collect references are unsupported.

Baseline10 tests/29.840s. Final **137 passed/40.784s**, no skips, `-W error`.
Coverage includes writer waits and caller mutation, replay/tombstones, corrupt
references, partial append/fsync/index failures, CURRENT publish interruption,
concurrent reads/compaction, upload complete/abort, exact quorum receipts,
accepted RPC drain, durable lost metadata ACK across restart, pending-recovery
failure blocking GC, and old generation rejection after a duplicate PUT.

The six-process feature demo verified owner buckets1/other owner0; CA/ and NY/
common prefixes plus federal.txt in an unchanged one-item-page snapshot despite
a new ZZ/ object; two recursive CA matches; concurrent multipart completions
returning one version; completed abort409; stale sweep409; verified downloads;
zero unrecoverable chunks. [Feature JSON](../benchmarks/features-2026-10-26.json).
Original demo:1,000,000-byte multipart, historical version and one-node-down reads
all matched, no unrecoverable data; elapsed3986.690ms.

Four-client40-object sample:PUT1.046s/38.23ops/s,p50/p9599.655/124.262ms;
GET0.520s/76.98ops/s,p50/p9548.046/69.440ms. All objects verified byte-for-byte,
40 packed chunks/node,one pack/node,166310pack bytes/node,no large files and no
unrecoverable chunks. [Raw results](../benchmarks/review-2026-10-26.json).
One localhost burst is not a controlled speedup comparison, sustained throughput,
independent-failure-domain durability or100PB/six-nine/four-nine SLA proof.

Graph: final incremental update6files/no errors after initial15file refresh.
Six existing core files79direct nodes/20impacted nodes/5additional files,
344edges truncated,2093unresolved calls. Newly untracked chunk_io.py explicitly
not indexed; source/independent review/execution covered new modules. No git
staging was performed to alter graph coverage.

Remaining: placement heartbeats/consensus, independent AZs, catalog/metadata HA,
multi-pack rotation/per-core streams, GB streaming, erasure coding, autonomous
repair, online resharding, IAM/TLS/encryption and lifecycle/backup/scale testing.
No AWS service, commit or push was used.

Bucket deletion also rejects pending publication intents with409, preventing
recovery from publishing an inaccessible version into an already removed bucket.

The final delete-guard update refreshed2 additional graph files with no errors.
Static acceptance:62 Python3.10 syntax parses,62 Ruff checks/format checks,
20 chapter Markdown files with zero missing local links, and git diff --check.
