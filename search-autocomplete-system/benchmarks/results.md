# Local latency measurement

2026-10-02: Windows 11, Python 3.13.9, 16 logical CPUs. Standard-library SQLite
in-memory store, 10,000 distinct synthetic queries, four balanced shards.
The benchmark made 10,000 sequential local requests and 500 actual HTTP requests
against one server holding all shards. This is not a concurrent distributed QPS test.

| Metric | Result |
| --- | ---: |
| Batch build | 59 ms |
| First cold lookup (including trie load) | 48.37 ms |
| Local p50 / p95 / p99 | 0.011 / 0.0191 / 0.0225 ms |
| HTTP p50 / p95 / p99 | 1.102 / 1.6298 / 22.6696 ms |
| Sequential local lookups per second | 74,648.93 |

All measured cold/percentile latencies were below the chapter's 100 ms target in
this run. This does not establish a production latency SLO, 48,000 concurrent
network QPS, or 10-million-user capacity. Filtered subtree refill and cold builds
are workload-dependent. Raw report: [latency.json](latency.json).

Correctness suite: **30 passed**. Actual HTTP replica fallback and an independent
process kill were exercised. The network demo started three shard processes and
one coordinator, preserved the global result after one replica termination, and
applied an immediate filter. Python 3.9 syntax was checked; runtime used 3.13.

Reproduce after installation:

```bash
python tools/benchmark.py --queries 10000 --requests 10000 --http-requests 500
```
