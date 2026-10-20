# Validation and local measurements

Date: 2026-10-20. Python 3.13.9, Windows 11 build 26100. Source: this chapter's standard-library implementation. Raw benchmark output: [results.json](results.json).

## Correctness and real process faults

```powershell
New-Item -ItemType Directory -Force .runtime/tmp | Out-Null
$env:TEMP = (Resolve-Path .runtime/tmp).Path
$env:TMP = $env:TEMP
python -m unittest discover -s tests -v
```

**11 tests passed in 24.818 seconds**: 7 storage/aggregation unit tests and 4 network integration tests. The network lab runs six independent subprocesses with HTTP transport, a private SQLite raw log, four distinct SQLite result stores and a query API. It does not substitute shared memory for those service boundaries.

| Test area | Evidence |
| --- | --- |
| Stable identities and atomic ingestion | Same event ID returns its original position after log reopen; conflicting IDs roll back the entire batch |
| Validation and backpressure | Invalid IP/type/time/country/extra fields rejected; capacity rejects all-or-nothing and still accepts exact duplicates |
| Hot key distribution | 200 events for one ad spread across all four partitions; sum remains exactly 200 |
| Transaction rollback and replay | Injected exception before commit leaves count/checkpoint unchanged; restart and replay count exactly once |
| Corruption/lineage/gap handling | Bad payload checksum, source generation, and missing positions fail without advancing output |
| Watermark, late corrections, dimensions | Old closed windows revise; late count increments; intersection filters and half-open boundaries verified |
| Query budget | More than 10,000 candidates fails instead of silently approximating |
| Exact global Top N | On each of four shards hot=6 and a different local winner=9; global winner is hot=24, which local top-1 truncation would miss |
| Actual HTTP ingestion and authorization | Duplicate requests preserve hot=120/cold=45; count/Top N and country/user filters work; wrong-role tokens fail; incomplete source topology fails |
| Actual process exit during transaction | A separate child exits with code 73 via `os._exit` inside the pre-commit hook. Reopening its database shows zero output and checkpoint 0; normal worker replay produces exactly 25 |
| Source restart | Kill/restart ingress on retained local disk, resend all 25 events, get only duplicate receipts and unchanged count/lineage |
| JSONL, late revision, replay, reconciliation | Partial final line waits; two late events then a third change checkpoint/count from 2 to 3; rebuilt per-partition hashes match live stores |
| Independent audit catches corruption | Modify online dimension counts while raw events remain unchanged; independent raw batch reconciliation returns `matches=false` |

The pre-commit exception is an injected unit failure; the child exit and service restarts are actual independent process failures. They retain local disks and do not test machine/storage loss or broker quorum failover. A lost HTTP ACK is covered by identical request replay semantics; no network proxy packet-loss experiment is claimed.

The first restricted run could not use the sandbox system temp directory; the workspace-local temporary directory resolved that environment issue. A combined integration run also exposed that the lab's catch-up check considered only offsets after ingress restart, while workers could still report their last transient source error. The lab now waits for both caught-up offsets and no current worker error. The service correctly returned 503 during that recovery interval. The full corrected suite passed above. A test time range was subsequently anchored before ingestion to avoid a minute-rollover race.

## Network demo

```powershell
python examples/network_demo.py
```

Observed: six processes; 100 unique events; the second request returned 100 duplicate receipts; query yielded hot=80 and other=20; all partition lag estimates were zero and independent reconciliation matched. Time from initial push through duplicate push and query: **358.44 ms**. This includes polling and is one observation, not a latency percentile or service SLA.

## HTTP pipeline benchmark

```powershell
python tools/benchmark.py --output benchmarks/results.json
```

| Measurement | Observed |
| --- | ---: |
| Unique events / log partitions | 3,000 / 4 |
| Hot-ad share | 70% |
| Partition event counts | 751 / 738 / 745 / 766 |
| Publish wall time | 0.599 s |
| Accepted events per second | 5,007.97 |
| First publish through all workers caught up | 0.736 s |
| Batch acceptance p95 (15 sequential batches of 200) | 55.929 ms |
| Query requests / concurrent clients | 40 / 4 |
| Query p50 / p95 | 58.923 / 81.294 ms |
| Query requests per second | 65.92 |
| SQLite file bytes at measurement | 1,347,584 |
| Count and complete-dimension reconciliation | Matched |

Ingestion uses real localhost HTTP writes to the persistent raw-log service. Four independent workers consume over HTTP, and the query process merges all partitions. Queries have a fixed three-minute range and no application result cache. OS page caches are warm. All benchmark responses are checked against the expected 3,000 clicks, and an independent batch dimension audit verifies the result. The run overlapped some correctness-test execution, so CPU/disk contention can affect its values.

This small localhost burst does not establish sustainable production throughput, billion-event capacity, cross-machine latency, high-cardinality worst-case performance, replicated durability, privacy compliance, or availability. Ingress is a single SQLite log owner; worker databases and raw log are not replicated. Raw events and dedup keys have no automatic retention; dimension storage may approach one row per event. Before production, use replicated transport and storage, explicit resharding/generation promotion, bounded distributed batch jobs, privacy retention and measured multi-host load/fault tests.
