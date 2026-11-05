# Validation and local measurements

## Source re-review — 2026-11-04 (Asia/Shanghai)

Re-read chapter 22 and inspected raw storage/aggregation, tailer/checkpoints,
recovery and query/HTTP via CodeGraph. The [source comparison](../docs/open-source-review_cn.md)
links actual pinned PyFlink adtech and Flink window code, and distinguishes their
join/dedup/late-state semantics from the local reference.

Two minimal fixes classify deep JSON faults as 503: persisted raw-row corruption
retains the data/head and refuses a partial page; required source/worker response
decode-depth errors no longer become client-error 400. Query finally still drains
accepted work/releases admission, and the same service recovers with a healthy
peer. Invalid client input remains 400.

### Strict regression

Python 3.13.9 / Windows 11 build 26100, standard library/SQLite. Real HTTP/process
and OS-lock/hard-link tests used approved unrestricted execution and workspace
temporary paths. From this project with PYTHONPATH=src:

```powershell
python -W error -m unittest discover -s tests -v
```

Before changes, **76 tests passed in 68.043s**. New tests reproduced persisted
decode RecursionError and valid HTTP queries returning 400 for deep source/worker
responses. After the fixes, **78 tests passed in 38.131s, no skips, all warnings
treated as errors**. The historical groups below remain, with storage/file tests
increasing from 30 to 31 and HTTP from 14 to 15.

The storage regression uses real SQLite corruption/reopen and confirms retained
data/head with no partial read. The HTTP regression uses actual sockets/API and
controlled source/worker responses, then confirms capacity=1 recovers on the same
query service. Original independent-process fault tests still cover pre-commit
exit, restart, Top N and reconciliation. These scopes remain distinct.

### Actual six-process example

```powershell
python -W error examples/network_demo.py
```

Passed with **100 unique clicks / 100 duplicate receipts**, hot=80 / other=20,
all 4 partition lag estimates zero and matching independent reconciliation.
Push, duplicate push and query took **498.17ms**, a single observation rather
than a percentile. Owned services stopped; diagnostics remain ignored under
`.runtime/lab-*`. No external broker/cloud service was used.

### Small pipeline/query observation

```powershell
python -W error tools/benchmark.py --output benchmarks/review-2026-11-04.json
```

The final benchmark followed regression/example without concurrent tests or graph
builds. [Raw report](review-2026-11-04.json):

| Measurement | Observed |
| --- | ---: |
| Processes / partitions | 6 / 4 |
| Unique clicks / hot-ad share | 3000 / 70% |
| Accepted events per second | 2391.23 |
| Publish duration / all processed duration | 1.255 / 1.578s |
| Batch acceptance p95, 15 sequential batches | 121.473ms |
| Query requests / clients | 40 / 4 |
| Query p50 / p95 | 94.116 / 125.561ms |
| Queries per second | 39.78 |
| Partition counts | 751 / 738 / 745 / 766 |
| SQLite bytes at measurement | 1,724,472 |
| Query totals and independent complete-dimension audit | Matched |

Every query is checked against all 3000 events. Queries use a fixed range without
a query cache, with warm OS pages. Query p95 is sorted index 37 of 40 samples;
batch p95 is index max(0,floor(0.95×n)−1) of 15 samples. Acceptance throughput is
not end-to-end processing throughput. The fixes concern fault boundaries, with
no speedup or production-capacity claim. Initial baseline/specialist checks may
share host resources; historical samples and their timing are not a controlled
before/after comparison, sustained SLA or power-loss test.

### Static, graph and documentation checks

Ruff E9/F passed for `src` and both changed/new test files; all 4 changed Python
files passed formatting. All 27 project Python files parsed with Python 3.10
syntax rules. Local checks validated 528 links in 16 Markdown files and parsed
benchmark JSON. All 27 authored guides matched generated output; the chapter's
3 Mermaid diagrams rendered in a headless browser. Scoped `git diff --check` passed.

Graph refresh re-parsed 3 files, updating 59 nodes/694 edges without errors.
Two-file scoped direct impact reported 21 file nodes, 74 impacted nodes and 13
additional files, showing 60 impacted nodes and omitting 14; 253 edges were
truncated and 764 calls unresolved. Source/execution checks supplement these
structural hints and the new untracked regression; the graph is not a complete
dynamic-dispatch/test-coverage proof. See the source comparison for reviewed paths.

## Historical core review — 2026-10-26 (Asia/Shanghai)

Python 3.13.9 / Windows 11 build 26100; standard library only. Real localhost
fixtures and Windows OS-lock/hard-link tests used approved unrestricted processes.
Owned services were stopped and temporary data isolated inside the workspace;
private diagnostic data remains ignored under `.runtime/lab-*`.

### Final regression

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -q
```

**76 tests passed in 22.816s, no skips and no ResourceWarning.** The existing
11-case suite also passed earlier in this review in 19.044s. Final coverage:

| Area | Tests |
| --- | ---: |
| Original storage/aggregation and actual six-process network faults | 11 |
| Raw persistence, source validation, replay receipts and checkpoint files | 30 |
| Bounded query pool, exact reduction and independent recovery/audit | 14 |
| Actual HTTP/RPC admission, framing, deadlines and drain | 14 |
| CLI and worker ownership | 7 |

New storage/file tests use actual SQLite and filesystem operations, including
locking, exclusive temporary writes, same-file/hard-link checks, lost confirmation,
incomplete lines, request ownership, clock changes and transaction rollback. New
offset receipts bind sequence to identity/hash/accepted time; historical databases
without that table can only validate their older positions through seen ID/hash.
Audit's streaming JSON digest is checked against the independent original encoding.

Query/recovery tests explicitly substitute RPC responses while using real executors
and local databases. They cover exact global Top N, complete source topology,
malformed/failed workers, admission and shutdown, cancellation/overall deadline,
candidate budgets, verified replay pages and independent count mismatch. A review
found worker-side 422 was being converted to 503: the final implementation preserves
that known worker query-budget error, with a specific regression. Other required
peer failures still return 503. The original network fixture separately verifies
actual HTTP and process crashes/restarts, not shared-memory substitutes.

### Actual network demonstration

```powershell
python examples/network_demo.py
```

The six independent processes accepted **100 unique events**, returned **100
duplicate receipts**, and reported hot=80 / other=20 with all four lag estimates
zero and independent reconciliation matching. Push, duplicate push and query took
**356.01ms** in this observation; it is not a latency percentile.

### Current small HTTP pipeline measurement

```powershell
python tools/benchmark.py --output benchmarks/review-2026-10-26.json
```

This final measurement ran after the final regression and demo, without another
suite or graph build running concurrently. [Raw report](review-2026-10-26.json):

| Measurement | Observed |
| --- | ---: |
| Processes / partitions | 6 / 4 |
| Unique clicks / hot-ad share | 3000 / 70% |
| Accepted events per second | 5631.68 |
| Publish duration / all processed duration | 0.533 / 0.778s |
| Batch acceptance p95, 15 sequential batches | 43.450ms |
| Query requests / clients | 40 / 4 |
| Query p50 / p95 | 60.902 / 78.452ms |
| Queries per second | 65.8 |
| Partition counts | 751 / 738 / 745 / 766 |
| SQLite bytes at measurement | 1,687,552 |
| Query totals and independent complete-dimension audit | Matched |

Every benchmark response is checked against all 3000 clicks. Query range is fixed
and uncached, with warm OS pages. Timing includes real HTTP, raw-log persistence,
worker traffic and exact global reduction; query checks and audit follow ingestion.
There is no controlled before/after speedup ratio, sustained production throughput,
cross-machine SLA, replicated durability or financial exactly-once guarantee.

### Graph evidence and implementation boundaries

code-review-graph minimal context, incremental refresh and impact analysis succeeded.
Seven existing core files reported 57 directly changed nodes, 25 impacted nodes,
five additional files and 1244 unresolved call sites, with truncated edges. New
untracked `query_service.py` was explicitly not indexed. Source inspection,
independent review and executed behavior therefore supplement graph navigation;
other chapters' existing dirty work remains in repository-wide context.

The API now owns a bounded shared pool rather than one executor per request. Its
default is eight threads/eight admitted queries/five-second shared RPC budget;
admission remains held until accepted tasks really exit. HTTP defaults to 32
handlers/128 backlog/10s, with 1MiB request and 8MiB response limits. Reconciliation
and replay default to 60s/200000 events; reconciliation dimensions are capped at
100000. Audit streams at most one million rows. See [chapter mapping](../docs/chapter22-review.md).

Single raw-log/SQLite ownership, static partitions, no second output queue or
distributed commit, no machine/disk loss recovery, unbounded lifetime of raw/dedup
metadata and no WAN/cloud/TLS/financial settlement remain explicit extensions.
Socket timeouts cannot forcibly terminate accepted DNS/SQL/CPU work. Process tests
retain local disks and do not establish universal filesystem power-loss behavior.

## Historical verification — 2026-10-20

Date: 2026-10-20. Python 3.13.9, Windows 11 build 26100. Source: this chapter's standard-library implementation. Raw benchmark output: [results.json](results.json).

### Correctness and real process faults

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

### Network demo

```powershell
python examples/network_demo.py
```

Observed: six processes; 100 unique events; the second request returned 100 duplicate receipts; query yielded hot=80 and other=20; all partition lag estimates were zero and independent reconciliation matched. Time from initial push through duplicate push and query: **358.44 ms**. This includes polling and is one observation, not a latency percentile or service SLA.

### HTTP pipeline benchmark

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
