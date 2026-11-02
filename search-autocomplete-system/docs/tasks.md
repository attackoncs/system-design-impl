# Tasks: Search Autocomplete System

- [x] T1: Read Chapter 14 and write requirements, design and tasks before implementation.
- [x] T2: Implement normalized queries, deterministic cached-top-five trie and LRU.
- [x] T3: Implement durable event collection, deduplication and window aggregation.
- [x] T4: Implement atomic versioned snapshots, balanced shard maps and block rules.
- [x] T5: Implement local service, snapshot reload and multi-shard global merge.
- [x] T6: Implement bounded HTTP replicas/coordinator, CLI and process demo.
- [x] T7: Verify correctness, actual network failover and version fences.
- [x] T8: Measure local latency and update English/Chinese documentation and root index.
- [x] T9: Record exact verification results; leave changes uncommitted pending instructions.

- [x] T10: Compare Sonic/RediSearch source; preserve local frequency ranking and generation publication contracts.
- [x] T11: Reject incomplete Content-Length responses and fail over; support legal close-delimited bodies.
- [x] T12: Reuse finite-number validation across collection, worker and HTTP configuration; huge integers raise ValueError.
- [x] T13: Close HTTPError/SQLite test resources; retry transient Windows locks during owned demo-directory cleanup.
- [x] T14: Update bilingual docs, reading guide, raw measurements and root index; [latest review](open-source-review_cn.md).

## Verification

- 2026-11-02: 188 passed (170 existing + 18 new), strict warning mode; actual HTTP/process failure and both examples verified, including demo cleanup.
- Local/HTTP latency measured; benchmarks/results.md records scale limitations.
- Changes remain uncommitted and unpushed.
