# Chapter 24 source and concurrency review — 2026-11-05

Read the complete local chapter (lines11227–11924), compared pinned Stalwart
and maddy source, and used CodeGraph to inspect mailbox, replication,
SMTP/spool/worker, objects and HTTP ownership. See the
[source audit and chapter matrix](open-source-review_cn.md).

Confirmed defect: positive-wait mailbox notifications could occupy all HTTP
workers, preventing ordinary receive/CAS writes from producing new events.
The minimal fix changes only `http.py` and `server_runtime.py`: per instance,
positive-wait notifications may hold `floor(3 * concurrency / 4)` permits.
Default32 permits24 notifications and leaves8 ordinary admission slots.
Excess notifications return503 without queueing; concurrency1 rejects positive
waits but supports `wait=0`. Authentication and finite0..2-second wait validation
precede admission. The owning request thread returns the permit after the
response ends, including errors/cancellation. This is not per-user rate limiting
or a guarantee of progress under arbitrary ordinary load.

Three new tests cover primary/API saturation, real SQLite CAS wake-up,
invalid waits, zero-wait reads, one-worker behavior and release after errors.
The pre-fix run failed three subcases (direct primary, API and one worker).
The observer only synchronizes entry to `MailboxViews.changes`; real mailbox
reads, writes and HTTP forwarding execute normally. The error-cleanup case
explicitly injects a business409.

Baseline **96 tests passed in32.220s** (2026-11-04). Final **99 tests passed
in31.374s**, no skips, `-W error`; the16-test HTTP suite passed in2.105s.
Two actual nine-process examples passed: original SMTP/attachment/replay demo
763.96ms to replica search; durable notification363.629ms, resumed versions[2,3],
replica barrier version2, wrong-lineage409 and zero outsider events.
Forty local messages all reached the SMTP spool and replica search; see
[measurements](../benchmarks/results.md),
[performance JSON](../benchmarks/review-2026-11-05.json) and
[feature JSON](../benchmarks/features-2026-11-05.json).

Corrected older design text denying the implemented read barrier and clarified
the network sender's30-second lease versus the two-second direct helper default.
SMTP has only a five-second idle timeout, not a total session budget; an active
connection may retain a slot. All mail remains synthetic local.test traffic;
scanner markers are not AV. Cc/Bcc/threading, full folder management, public
deliverability, replicated objects/spool, write failover and billion-user/multi-host
validation remain gaps. No commit or push was performed.

Graph evidence: the incremental build re-parsed5 files, updated105 nodes/1378
edges and reported no errors (these are update counts, not repository totals).
Explicit direct two-hop impact for the two changed production files reported
17 directly changed nodes,15 impacted nodes and2 additional files, without
truncation;271 unresolved call sites remain. Static graph results guide source
review and do not prove complete coverage or correctness.

---

# Historical Chapter 24 review — 2026-10-26

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
