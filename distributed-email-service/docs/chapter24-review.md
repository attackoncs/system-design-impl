# Chapter 24 review — 2026-10-26

Scope: local book lines11227–11924. Sending/receiving, immutable objects,
versioned read/delete and fielded search existed. Added fixed folders, durable
user notifications, optional read barrier and bounded metadata cache. Reworked
record/replication/spool/transport ownership; removed thread/resource races and
stale lease settlement. See the READMEs for exact API bounds and exclusions.

Baseline: 9 tests,21.754s. Final: **96 passed,30.741s**, no skips, `-W error`.
The initial expanded run exposed a leaking legacy-test SQLite handle, Windows
socket cancellation, obsolete SMTP AUTH test identity and duplicate object
publication. These were fixed before final acceptance. The standard-library
HTTP target normalization is checked against the raw request line.

Actual nine-process feature evidence: notification342.560ms, one inbox event,
same send ID on replay, unread1 before marking, replica version2 with a barrier,
wrong-lineage409, resumed versions[2,3], deletionversion3, outsider events0,
cachehits1. See [feature JSON](../benchmarks/features-2026-10-26.json).
Original demo: one unique inbox, attachment bytes match, replica search,
send-to-search535.690ms, test-markers-only scanner.

Forty sends/four clients: admission1.002s (39.93messages/s), all forty delivered
and replica-searchable4.973s (8.04messages/s). Forty replica searches/four clients:
p50/p9543.571/58.355ms,93.04requests/s. The LRU may benefit repeated searches;
there is no controlled before/after experiment, sustained capacity or SLA proof.
[Raw measurements](../benchmarks/review-2026-10-26.json).

Graph review: minimal context and explicit chapter-file analysis used; final
incremental build/impact evidence is recorded below. Newly untracked modules may
be excluded by the graph; source review and executed tests cover those paths.

No external mail or production AV providers were used. HA write failover,
replicated objects/spool, public deliverability, privacy lifecycle, Cc/Bcc,
conversation threading, full folder management and billion-user load remain gaps.
No commit or push was performed.

Final graph update reparsed36 files without errors (includes next-chapter work).
Six existing email core files:64 directly changed nodes,11 impacted nodes,
2 additional files;163 edges truncated,1378 unresolved calls. New untracked
mailbox_views.py explicitly returned target-not-indexed, so graph absence is
not proof of no callers. New modules were independently read and executed.
