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
