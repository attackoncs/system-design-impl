# Chapter 26 source and implementation review — 2026-11-05

Read the local chapter (lines12814–13481), compared pinned WB-Games leaderboard
and Redis8.0.5 source, and inspected CodeGraph paths for journal, profiles,
ranking, projection contracts/Lua, RESP, HTTP and runtime ownership. The bounded
core flows are present; no confirmed new production-code defect was found.
Production code and tests are unchanged in this review. See the
[source audit and chapter matrix](open-source-review_cn.md).

Core coverage: trusted unique wins, monthly zero-score membership, fixed Top10,
competition ranks and display-position neighbors; durable match deduplication,
absolute-score replay and source-head/epoch barriers. Profile caches contain
names, not stale board scores. Query snapshots share source read ownership;
mutations, reconciliation and rebuilds are exclusive. Internal feed/status
bypass public operation capacity so projections can satisfy barriers.

The local choice is application-level user hashing across independent Redis
primary/replica pairs. It is not Redis Cluster CRC16 slots or automatic topology
migration. Score-range partitions and DynamoDB are alternatives in the book,
not required simultaneous backends; no extra backend was added just to mirror
every design alternative. Stable user-ID order is used for ties, rather than
the optional achieved-score timestamp tie-breaker.

Current strict validation: **72 tests passed in36.409s**, no skips, `-W error`.
This is one full acceptance run, not separate before/after runs. Actual existing
Redis8.0.5 in WSL was verified before use. Tests include real four-Redis/seven-process
integration, primary hard exit/manual promotion, AOF reopening with projection
stopped, full Redis loss/replay, independent reconciliation and epoch resets.
Pure-memory RESP tests and SQL-backed RPC unit tests are separate supporting
evidence, not substitutes for Redis integration.

The seven-process demo produced ranks[1,1,3,3], scores[1,1,0,0], detected a
duplicate and matched SQL/Redis reconciliation; observed551.54ms for the measured
two-win/duplicate/query scenario. The benchmark fully projected60 wins for21
registered players, then ran40 Top10 and40 personal/neighbor queries with4 clients.
No production-code change or controlled performance improvement is claimed.
[Feature JSON](../benchmarks/features-2026-11-05.json),
[performance JSON](../benchmarks/review-2026-11-05.json),
[measurements and scope](../benchmarks/results.md).

Graph update completed without errors and reparsed0 files because no source
changed. Dependency analysis explicitly targeted service.py, ranking.py and
projection.py:43 target nodes,21 two-hop impacted nodes and4 additional files;
77 edges were truncated and896 unresolved call sites remain. The tool labels
manual targets as changed nodes; these are review targets, not a code diff.
The old approval-blocked graph note below is historical. Current graph/source
inspection and executed tests do not imply complete static coverage.

Corrected stale serialized-query wording and task dates, updated bilingual
requirements/design/tasks, source indexes and the macro-to-source guide. No
staging, commit or push was performed. Authority HA, automatic Redis election,
TLS/full accounts, anti-cheat, online sharding and retention remain gaps;
10000 players/10^9 wins are reference bounds, not25M-MAU/2500-score-QPS proof.

---

# Historical Chapter 26 review — 2026-10-27

Book lines12814–13481. Existing core features were implemented. Added bounded
coherent concurrent rank reads and source-revision profile caching; hardened
journal, contracts, Lua/RESP/transport and lifecycle. READMEs specify the bounds.
The hashing solution is implemented; score-range and DynamoDB are alternatives,
not simultaneously required backends. No production HA/scale claim is made.

Baseline8tests38.686s from isolated HEAD source. Final72passed39.110s, no skips,
-Werror. Actual Redis8.0.5 in WSL:primary crash/manual promotion/AOF reopen,
Redis loss/replay, independent reconciliation, cross-shard ties/neighbors,
Lua prevalidation, epoch resets and idle AOF tested. Protocol fake peers only
exercise framing/cancellation; they do not substitute for Redis integration.

Original seven-process demo:ranks[1,1,3,3],scores[1,1,0,0],one duplicate,
reconciliation true,742.600ms.60wins21players:8.153s/7.36projected wins per second,
p50/p95141.863/197.049ms.4clients40requests each:Top10 118.700/146.256ms,32.93QPS;
rank+neighbors207.717/240.098ms,18.68QPS. Journal SQLite-only bytes81920 excludes
WAL/SHM and is not complete disk usage. [Raw sample](../benchmarks/review-2026-10-27.json).
This is not a controlled speedup comparison, sustained capacity or original-scale
validation. Changes retain single authority and require operational primary fencing.

Graph context was consulted and final incremental/impact evidence is recorded
below. New untracked modules can be absent from the graph; source, independent
reviews completed before the interruption, and executed tests cover those paths.
No staging, commit or push was performed.
Final graph build/impact was blocked by the tool approval policy:
'MCP tool call requires approval,but approval policy is never'. Initial cached
minimal context was available (6863nodes/62421edges/544files,2026-10-26T20:03:03).
No final freshness or complete graph coverage is claimed; source call-chain
review and execution were used for untracked/extracted modules. No approval
bypass, git staging, commit or push was attempted.
