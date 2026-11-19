# Chapter 9 correctness workload

Final verification: 2026-11-01. The earlier [2026-10-31 result](review-2026-10-31.json) is retained. [Raw result](review-2026-11-01.json).

```bash
python tools/verify_concurrency.py --output benchmarks/review-2026-11-01.json
```

Four independent processes share one SQLite file. Each owns a `SQLiteStorage` and uses that same object both as mapping storage and as `Base62Strategy`'s ID generator. Each process creates 125 distinct URLs and repeatedly creates one URL shared by all processes.

| Check | Observed |
|---|---|
| Shorten calls | 1000 |
| Unique URL/code pairs | 501 |
| Shared URL deduplication | Passed |
| All mappings resolved after reopening | Passed |
| New ID after restart | No reused code |
| Actual HTTP statuses | 200 create, 302 Unicode redirect, 400 invalid URL, 404 missing |

The raw elapsed time includes Windows process startup, database creation and connection overhead. This is a correctness workload, with no before/after throughput claim and no production capacity claim. SQLite writes remain serialized. The stdlib WSGI demo server is single-threaded; use a production WSGI server for concurrent HTTP.

## 2026-11-17 复核

基线145项/1.90秒，最终151项/2.40秒，Python3.14.8、-W error、无跳过。语句覆盖97.33%、分支86.59%、综合95.40%，未排除业务模块。新6项5个RED、1个既有合法拒绝通过；独立审查无重要遗留问题。既有测试及benchmark代码未改。

原有`tools/verify_concurrency.py --output benchmarks/review-2026-11-17.json`真实4进程1000次创建，恰好501个唯一映射；重启解析和新ID检查通过。真实HTTP返回200/中文302/非法400/缺失404。8线程4000次点击分别验证明细上限0、1、None及精确计数。单机小样本不证明部署吞吐，没有配对性能基线。

[原始集成结果](review-2026-11-17.json) · [覆盖与回归记录](verification-2026-11-17.json) · [章节](../docs/chapter09-review.md)。
