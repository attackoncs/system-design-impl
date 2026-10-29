# Requirements: Real-time Gaming Leaderboard

Based on Chapter 26 of the root-directory "System Design Interview.md". The goals are monthly-season scoring, Top 10, personal competition rank, and the four neighboring display positions before and after; the book's 2,500 peak score-increment QPS / 25 million MAU figures are unverified production targets.

1. A trusted match service submits a unique match ID, winner, 2–8 participants, and end time; each win gives exactly +1, and client-reported points are not accepted.
2. All registered players enter the current month's season, including zero-score users. Months are UTC-based; a new ranking starts at natural month boundaries without clearing historical data.
3. The ending month determines ownership, with up to 48 hours of lateness allowed after month end; after closure, only same-content retries of already-processed events are accepted. Future skew is at most 30 seconds, and matches with participants not yet registered are rejected.
4. Tied scores share the competition rank `1 + count of strictly higher scores`; e.g., 1, 2, 2, 4. Display order breaks ties by ascending user ID; Top 10 holds exactly at most 10 people and does not expand tied groups.
5. The neighbor window is at most four display positions before and after, not four distinct score groups; it returns the competition rank and a unique display position.
6. The SQLite authoritative match/score log provides durable deduplication, and the Redis sorted-set is a rebuildable projection. Scores, events, and idempotent receipts commit in the same transaction.
7. Redis is sharded by user hash, with real primary/replica, AOF, and journal rebuild; partial rankings with a missing shard are forbidden, and standby nodes are not automatically guessed and promoted.
8. Reads and writes expose the distinction between durable acceptance and completed projection; normal writes wait for a short projection barrier, during failures the write is saved and answered with 202, and reads that cannot establish a complete barrier return 503.
9. Validate concurrent duplicates, ties/zero scores, cross-month lateness, commit rollback, real primary process exit/manual promotion/AOF recovery, independent reconciliation and rebuild; measure real latency.

The reference capacity limit is 10,000 registered players, with scores up to 1 billion. There is no real match anti-cheat, full account system, automatic Sentinel/consensus leader election, cross-datacenter Redis, authoritative database HA, or online resharding.

2026-10-27 acceptance:72 tests39.110s,real Redis8.0.5/seven processes.
Shared ranking snapshots, exact heads/epochs, bounded RPC/HTTP/RESP, revision
profile cache, frozen journal values, batched zero membership, stale-poll fences
and idle-AOF preservation verified. Production limitations remain documented.
