# Validation and local measurements

Date: 2026-10-21, Asia/Shanghai. Python 3.13.9, Windows 11 build 26100, actual Redis 8.0.5 in WSL Ubuntu-26.04. The test used an existing unpacked local executable and its library directory; that executable is not bundled or installed by this module. Raw benchmark output: [results.json](results.json).

## Correctness

Set `LB_REDIS_BINARY`, or the documented `LB_WSL_REDIS_BINARY` / optional `LB_WSL_REDIS_LIB` / distro variables to real executable paths, then:

```powershell
New-Item -ItemType Directory -Force .runtime/tmp | Out-Null
$env:TEMP = (Resolve-Path .runtime/tmp).Path
$env:TMP = $env:TEMP
python -m unittest discover -s tests -v
```

**Eight tests passed in 51.802 seconds**: five authoritative-journal tests and three real seven-process network tests. The processes are authority HTTP, two projection HTTP nodes and four independent Redis primary/replica instances. RESP commands, Lua, AOF and replication are actual Redis operations, not mocks.

| Area | Verified |
| --- | --- |
| Concurrent duplicate result | Twenty unit submissions / eight HTTP submissions of one match ID increment once; changed content conflicts |
| Atomic source | Injected pre-commit exception leaves source heads/scores/receipts unchanged; reopen preserves lineage and dedup |
| Month boundaries | UTC January/February rollover, prior-month late acceptance, exclusive late deadline, immutable close and old receipt replay |
| Membership | Zero-score players enter the season; later accounts cannot claim earlier matches; historical score reset preserves the prior month |
| Trusted ingestion | Read credential cannot submit scores; points override and malformed participants/timestamps are rejected |
| Exact global ties | Multi-shard Top 10 returns ranks 1,2,2,4,5,5,5,5,5,5 with deterministic IDs; neighbors match independent full sorting |
| Real primary exit | Verified owned Redis process receives a hard exit; queries return 503 until an operator promotes the replica |
| Fenced recovery | Old primary is restarted as a replica; post-promotion scores remain correct and SQL/Redis reconciliation matches |
| Actual AOF recovery | Stop projector, hard-exit/restart promoted Redis, inspect its saved score directly before restarting projection |
| Full Redis loss | Flush only wholly owned Redis, replay all authority events and recover the exact score |
| Corruption and rebuild | Deliberately modify both Redis indexes to a wrong score; independent audit detects mismatch and explicit namespace rebuild restores it |
| Source restart | Authority process reopens its retained journal and serves the same rankings after projection catch-up |

The initial replica run could not load RDB from the WSL-mounted workspace. Per-instance `repl-diskless-load swapdb` and `aof-use-rdb-preamble no` resolved that environment issue; no system kernel settings were modified. A failover run then showed an unavailable endpoint's role discovery consuming the normal Redis response timeout; the discovery deadline was shortened separately, and the final full suite passed.

The full run emitted a ResourceWarning for a failed HTTP response. The HTTP helper was subsequently changed to buffer bounded error data and close the original response before propagation; the existing tie/authentication/concurrent HTTP network case passed again in **23.818 seconds**, without that warning, verifying status/body semantics. This cleanup does not change ranking or measurement semantics.

Commit rollback and Redis index corruption are deliberate injections. Primary hard exit, manual promotion, AOF reopen and all inter-process traffic are real. These are retained-disk tests on one machine. There is no Sentinel election, host/disk-loss experiment, quorum durability measurement or claim that asynchronous replication preserves every acknowledged view update independently of the authoritative journal.

## Executable demo

```powershell
python examples/network_demo.py
```

Final measured demo: seven processes, four Redis servers, two score shards; duplicate detected, first match fully projected, independent audit matched. Scores `[1,1,0,0]` produced ranks `[1,1,3,3]`. The scenario (two accepted wins, duplicate request and query) took **1562.51 ms**. This is a single scenario, not one-request latency or an SLA.

## Measured HTTP/Redis workload

```powershell
python tools/benchmark.py --output benchmarks/results.json
```

| Metric | Observed |
| --- | ---: |
| Authority/projectors/Redis servers | 1 / 2 / 4 |
| Registered players / unique matches | 21 / 60 |
| Write wall time through projection barrier | 20.919 s |
| Fully projected matches per second | 2.87 |
| Write p50 / p95 | 341.714 / 536.146 ms |
| Requests per query kind / clients | 40 / 4 |
| Top 10 p50 / p95 | 1661.129 / 2043.169 ms |
| Top 10 requests per second | 2.41 |
| Personal rank plus four neighbors p50 / p95 | 2877.862 / 3662.957 ms |
| Rank/window requests per second | 1.32 |
| SQL/Redis reconciliation | Matched |
| Authority SQLite bytes | 77,824 |

Writes wait for Redis to reach the accepted source heads. Queries go through actual HTTP projection nodes and Redis role/progress checks, with one authority serializing cross-shard snapshot phases. The rank workload includes neighbor queries rather than only one local ZRANK call. Redis uses AOF fsync-always, and the run overlapped correctness testing. This intentionally unoptimized standard-library/local-WSL path has substantial latency and must not be reported as meeting the book's near-zero latency or 2500-score-QPS scale.

TCP_NODELAY and avoiding unchanged AOF writes are present, but these measurements do not demonstrate a performance gain: workload contention and per-request connection/role checks remain material. A production implementation would need connection pooling, a carefully fenced role/placement control plane, scalable snapshot coordination, authoritative DB HA, bounded retention and multi-host load/fault tests. No 25M-MAU, autonomous failover or production availability claim is made.
