# Source and error-boundary review — 2026-11-05

Python3.13.9, Windows11 build26100, real localhost HTTP only. No AWS/cloud
credentials or external storage services were used.
[Chapter review](../docs/chapter25-review.md),
[open-source audit](../docs/open-source-review_cn.md),
[raw performance](review-2026-11-05.json),
[raw feature evidence](features-2026-11-05.json).

## Correctness and error handling

Baseline137 tests passed41.699s; final **138 passed41.769s**, no skips, with
`-W error`. One new regression contains six actual upstream HTTP cases. The
two deeply nested409/503 cases disconnected before the fix; now they return
neutral503. All20 HTTP tests passed2.422s. Valid upstream409 and invalid inbound
JSON400 are preserved. The test uses real API/Service/Catalog and a controlled
peer, verifies a single publication request and retained intent; it does not
simulate an actual metadata commit. It is not a six-process fault benchmark.

## Actual six-process examples

Original demo: two metadata shards, three data nodes, write quorum2; historical
version and one-node-down reads match, multipart1,000,000 bytes match, no
unrecoverable chunks. Observed4067.54ms includes the writes, retained-node restart,
multipart and maintenance; it is not an isolated upload latency.

Feature demo: owner buckets1/other owner0; common prefixes CA/ and NY/, object
federal.txt remain on the original one-item-page snapshot after a later ZZ/
write. Recursive CA matches2; two parallel parts and two completions return the
same version; aborting completed upload409, stale generation collection409,
downloads match and unrecoverable chunks0.

## Four-client object measurement

| Metric | Observed |
| --- | ---: |
| Processes / metadata shards / data nodes / quorum | 6 / 2 / 3 / 2 |
| Objects / clients / object bytes | 40 / 4 / 4105–4106 |
| PUT seconds / operations per second | 1.039 / 38.51 |
| PUT p50 / p95 milliseconds | 97.613 / 130.478 |
| GET seconds / operations per second | 0.541 / 73.95 |
| GET p50 / p95 milliseconds | 53.437 / 77.185 |
| Verified exact object reads | 40 |
| Sampled data-0 packed chunks / pack files / pack bytes | 40 / 1 / 166310 |
| Sampled data-0 large files / maintenance unrecoverable chunks | 0 / 0 |

The raw JSON's `*_per_node` pack fields are sampled from data-0, not independently
measured on all three nodes. The healthy loopback configuration tries all three
nodes; the publication threshold remains two valid durable receipts. p95 is
sorted index37 of40 samples. Tests and examples completed before this benchmark;
both examples and the benchmark ran sequentially. This short burst is not a
controlled speedup comparison, sustained capacity, independent-failure-domain
durability or100PB/six-nine/four-nine SLA proof. OS caches and other desktop
activity can affect results. Owned lab processes stop, while private configs,
disks and logs remain in ignored `.runtime/lab-*`.

## Static and documentation checks

Ruff E9/F passed for all project source and the changed HTTP test; both changed
Python files pass formatting. All32 project Python files parse with Python3.10
syntax rules. The16 reviewed Markdown files contain526 valid local links.
All27 authored guides match generated output. All three chapter25 Mermaid
diagrams rendered in an actual headless Edge browser; scoped `git diff --check`
passed. Tests are runtime evidence; graph/source/document checks supplement them.

## Reproduce without replacing historical evidence

From the project directory with `PYTHONPATH=src`; set TEMP/TMP to an accessible
private directory when needed:

```powershell
python -W error -m unittest discover -s tests -v
python -W error examples/network_demo.py
python -W error examples/listing_demo.py --output .runtime/features.json
python -W error tools/benchmark.py --output .runtime/benchmark.json
```

---

# Historical engineering review — 2026-10-26

137 tests passed40.784s, no skips, `-W error`. Both actual six-process demos passed.
Four-client40-object localhost measurement on Python3.13.9/Windows11:

| Metric | Result |
| --- | --- |
| PUT total / throughput |1.046s /38.23ops/s |
| PUT p50 / p95 |99.655 /124.262ms |
| GET total / throughput |0.520s /76.98ops/s |
| GET p50 / p95 |48.046 /69.440ms |
| Verified objects |40 |
| Packed chunks / pack files / bytes per node |40 /1 /166310 |
| Large files / unrecoverable chunks |0 /0 |

[Raw performance](review-2026-10-26.json),[feature protocol evidence](features-2026-10-26.json),
[review record](../docs/chapter25-review.md). The benchmark now uses four clients;
it cannot be compared as a controlled speedup to the historical sequential run.
The catalog adds durable intents to protect unknown publication outcomes.
Small localhost bursts and retained-disk faults do not establish an SLA.

---

## Historical 2026-10-20 report (sequential implementation)

# Validation and local measurements

Date: 2026-10-20. Python 3.13.9, Windows 11 build 26100. Raw benchmark output: [results.json](results.json).

## Correctness and faults

```powershell
New-Item -ItemType Directory -Force .runtime/tmp | Out-Null
$env:TEMP = (Resolve-Path .runtime/tmp).Path
$env:TMP = $env:TEMP
python -m unittest discover -s tests -v
```

**10 tests passed in 29.924 seconds:** six core tests and four real six-process HTTP tests. The lab runs an API/catalog process, two metadata shard processes and three independent data-node processes. All normal data/metadata access crosses HTTP.

| Area | Verified behavior |
| --- | --- |
| Packed small data | Twenty distinct small chunks share one pack; all read by offset correctly |
| Index crash gap | A fsynced pack record without an index entry is recovered on reopen |
| Incomplete tail | Injected partial final record is truncated; prior complete chunks remain readable |
| GC and compaction | Delete tombstones survive index rebuild; unreferenced large file disappears; compaction retains live bytes and reduces pack size |
| Corruption | Modified packed bytes fail SHA256 validation; verified rewrite appends a good record and survives reopen |
| Versions | Idempotent same-content publication, payload conflicts, delete markers, historical reads, marker removal and retention grace |
| Topology/ownership | Wrong metadata owner/topology, invalid manifests, foreign bucket/upload access and immutable part replacement are rejected |
| Write threshold | With two of three data nodes down, new nonempty upload returns 503 and never appears in listing; old objects read from the remaining copy |
| Multipart restart | Accepted first part survives API restart; incomplete completion fails; all parts produce exact 1,200,000-byte content |
| Publication intent recovery | Stop the owning metadata node before completion; completion becomes durable `completing`, returns 503, survives API restart and is resumed by maintenance without a second version |
| Expiry | An explicitly expired active upload is aborted/collected, while completed live multipart data remains intact |
| Logical node-disk loss | Stop a node and rename only its checked lab-owned data directory (original retained); fresh node is repaired from other replicas |
| Network repair | Corrupt one node and remove another's data; GET uses surviving bytes, maintenance repairs at least two copies, and all three nodes return exact original content |
| Stable enumeration | Freeze a three-item page, add a new object and create a later-key deletion marker; subsequent pages reproduce the original twelve sorted keys exactly |
| Token binding | Changed page size and altered token signature fail instead of changing pagination scope |

After the full suite, the diagnostic `copy_failures` collection was corrected to report failed repair writes rather than initial reads that were successfully repaired. The affected network case was rerun with an explicit empty-failures assertion:

```powershell
python -m unittest discover -s tests -p test_network.py -k corrupt_replica -v
```

**Passed in 6.295 seconds.** Its first attempted rerun was not executed because automatic approval review hit an account usage limit; after the user requested continuation the same approved execution path succeeded. The test and documentation do not treat that environment failure as a storage result.

Process exits/restarts and HTTP reads/writes are real. Byte corruption, partial records, advanced upload expiry and unavailable data directory are explicit fault injections. Directory renaming preserves original test bytes and is a logical disk-loss simulation, not actual physical disk failure. Tests do not cover independent hosts, every power-loss point, consensus/election or real availability zones.

## Demo

```powershell
python examples/network_demo.py
```

Observed six processes, two metadata shards, three data replicas, two-ack write policy. Historical version reads matched; latest reads worked with one node stopped; a 1,000,000-byte multipart object matched exactly; maintenance reported no unrecoverable hashes. Total scenario duration: **4069.06 ms**, including node restart, multiple writes/reads and maintenance. This is one scenario observation, not a single-request latency or SLA.

## Small-object HTTP benchmark

```powershell
python tools/benchmark.py --output benchmarks/results.json
```

| Metric | Observed |
| --- | ---: |
| Objects / clients | 40 / 1 |
| Object sizes | 4105–4106 bytes |
| Data nodes / publication threshold | 3 / 2 acknowledgements |
| Write wall time | 2.944 s |
| Writes per second | 13.59 |
| PUT p50 / p95 | 76.485 / 79.572 ms |
| Reads per second | 19.46 |
| GET p50 / p95 | 47.572 / 70.359 ms |
| Packed live chunks per node | 40 |
| Pack files per node | 1 |
| Pack bytes per node | 166,310 |
| Separate large files per node | 0 |
| Unrecoverable hashes on scrub | 0 |

Writes pass through actual HTTP coordinator, independent metadata shard and three data-node requests. Every read is checked byte-for-byte. The benchmark checks small-object packing and then scrubs replicas. Queries benefit from OS cache; no multi-client throughput claim is made. The run overlapped some correctness-test execution, so contention can affect measurements.

This short localhost workload does not prove sustained multi-host throughput, GB-scale streaming, 100 PB capacity, six-nine durability or four-nine availability. Three processes share one machine, catalog/metadata lack replicas, and maintenance is a bounded administrative full scan. Erasure coding, independent failure domains, metadata election, multi-host validation, lifecycle archival and production SLA measurement remain future work.
