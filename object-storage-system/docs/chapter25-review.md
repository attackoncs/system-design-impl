# Chapter 25 review — 2026-10-26

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
