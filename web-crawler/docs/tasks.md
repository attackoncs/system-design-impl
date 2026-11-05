# Chapter 10 Implementation and Acceptance

Updated against source and execution on 2026-11-05. See [requirements](requirements.md), [design](design.md) and the [book/source comparison](open-source-review_cn.md). The 2026-11-01 review is retained as history.

## Completed

- [x] Python>=3.9 src layout, zero runtime dependencies, typed config/models, public API and errors.
- [x] Bounded HTML download/deadlines/body size, truncation detection, download slots and TTL/LRU DNS.
- [x] HIGH/MEDIUM/LOW, FIFO ties, per-host reservation and completion-based delay.
- [x] Per-host heaps, ready heads and wake-up heap; no per-poll reserved-host backlog scan.
- [x] Randomized full-scan scheduling oracle and three identical-workload samples before/after.
- [x] Bound task creation; recheck due hosts every 50 ms with spare capacity/budget.
- [x] Origin robots cache/miss coalescing, Crawl-delay, wildcard/end-anchor and longest-rule matching.
- [x] Preserve escaped delimiters/literal operators; empty-path queries match from root.
- [x] First base href, per-link errors, host/port admission and case-sensitive userinfo preservation.
- [x] Separate URL/content identity; retry failed claims and discover links on duplicate content.
- [x] Single-owner durable queue, inflight recovery, atomic archive/acknowledgement, cancellation and durable retries.
- [x] Externally triggered adaptive recrawling, host ownership and durable routing contract.
- [x] Sync/async hooks and replaceable fetcher; explicit external-side-effect idempotence.
- [x] Close SQLite test connections and set async fixture loop scope; warnings treated as errors.
- [x] 2026-11-01: existing 213 plus 43 regressions, 256 tests; 2026-11-05 adds three repeated-cancellation cases, 259 tests, example and actual loopback/resumption checks.
- [x] Drain accepted SQLite work and owned children under repeated cancellation; close ownership and reset running state on open/finish/close cancellation.
- [x] Bilingual README/requirements/design/tasks, learning guide and [raw measurements](../benchmarks/REPORT.md).

## Deployment Extensions

- [ ] Membership, message broker, leases and versioned safe handoff control plane.
- [ ] Replication/sharding, five-year retention cleanup, capacity planning and geographical placement.
- [ ] Dynamic rendering, anti-spam models and PageRank/probabilistic scheduling.
- [ ] Full RFC robots agent-group merging and HTTP failure policies when required; compatibility behavior is explicitly retained.

These limits do not demonstrate the book's billion-pages/month capacity.

## Reproduce

```bash
pip install -e ".[dev]"
python -W error -m pytest tests -q
python examples/basic_crawl.py
python tools/benchmark.py --output benchmarks/local.json
python -W error tools/verify_http.py --output benchmarks/http-local.json
```
