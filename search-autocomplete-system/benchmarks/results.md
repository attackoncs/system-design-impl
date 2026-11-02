# 2026-11-02复验

Windows 11，Python3.13.9，16逻辑CPU。沿用现有工具，10,000条不同查询、四分片、10,000次本地查询和500次实际HTTP查询；[原始结果](review-2026-11-02.json)。

| 指标 | 结果 |
|---|---:|
| 批量构建 | 72.1ms |
| 首次冷查询 | 52.7201ms |
| 本地P50 / P95 / P99 | 0.0242 / 0.0371 / 0.0416ms |
| HTTP P50 / P95 / P99 | 15.2683 / 16.1922 / 16.4731ms |

这是单主机顺序请求，HTTP服务自身持有全部分片；没有测量跨进程协调器并发吞吐。此次修复侧重协议完整性和资源释放，不据这组数据作性能提升结论，亦不证明48,000峰值QPS或生产SLO。多进程示例另验证故障切换和即时过滤，188项回归通过。

复跑命令：`python tools/benchmark.py --output benchmarks/review-2026-11-02.json`。源码未安装时先设置PYTHONPATH为本项目src；网络测试只需本机回环。

## 早期测量记录

### Local latency measurement

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

## Chapter audit, 2026-10-24

Same workload shape: Windows 11, Python 3.13.9, 16 logical CPUs, 10,000 distinct
queries, four shards, 10,000 sequential local requests and 500 loopback HTTP
requests to one server holding all shards. Raw data:
[review-2026-10-24.json](review-2026-10-24.json).

| Metric | Result |
| --- | ---: |
| Durable aggregation plus snapshot build | 87.1 ms |
| First cold lookup including trie load | 66.3238 ms |
| Local p50 / p95 / p99 | 0.027 / 0.0439 / 0.0596 ms |
| HTTP p50 / p95 / p99 | 15.2289 / 16.5084 / 26.1677 ms |
| Sequential local lookups per second | 32,074.73 |

This run includes immutable snapshot construction, persisted aggregation, operation
ownership and a final database revision fence. Latencies are higher than the earlier
run; these separate sequential measurements do not establish the cause or a speedup.
The concurrency improvements are validated through controlled overlap, bounded-work
and shutdown regressions. No simultaneous-client or distributed-capacity benchmark
was performed. Full correctness suite: **170 passed**; both examples ran successfully.
