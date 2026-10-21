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
