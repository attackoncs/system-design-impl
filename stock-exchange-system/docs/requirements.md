# Chapter 29 Securities Trading System Requirements

Source: Chapter 29 of `System Design Interview.md` in the root directory. Simulated limit-order equity trading only, not connected to real trading venues; the original book's targets of one billion daily orders, millisecond-level round-trip latency with particular focus on p99, and four-nines availability remain unvalidated goals.

- Integer USD cent prices and integer share quantities; static synthetic accounts/positions; identity, session, and risk checks remain abstract.
- Idempotent new/cancel commands, totally ordered sequencing, price-time priority, resting-price execution, partial fills, self-trade prevention.
- Buyer cash and seller position freezing, spread refund, frozen-resource release on cancel; no short selling; cash and per-symbol share counts are conserved.
- Conservative per-user/symbol/UTC-day accepted-quota limit; cancellations still consume quota; simulated UTC weekday session; DAY orders expire at the first command of the following day.
- Independent logical book per symbol, shared single serial matching/account transaction; durable command chain, deterministic results, and snapshot/tail recovery.
- Independent primary/backup processes with asynchronous replay; manual promotion after candidates catch up; durable epoch fences old primary publication; sequencer is not replicated, no automatic leader election.
- Independent market/report projection, sequenced book deltas, snapshots, OHLCV, public-data desensitization, and private bilateral fill reports; returns 410 and resynchronizes when a window is missed.
- Independent process verification of fair ordering, cancel-versus-fill races, primary/backup/sequencer restarts, snapshot replay, cash/position conservation, and market recovery; reports measured p99 and boundary cases.

Does not include real timezone/holiday calendar, production KYC/compliance certification, bank settlement, fees, FIX, multicast, WebSockets, or cross-host durability.

- Add book-style aliases, authenticated symbol/order/time execution queries, candle windows and dense bilateral execution sequences.
- Price-level/FIFO indexing with explicit whole-state copy costs; audit owner flows, fill quantities and indexes, blocking matching after drift.
- Freeze before writer waits, reject damaged durable deduplication indexes, verify full promotion state and publication/ingestion epochs/prefixes.
- Atomically persist market cursor/fingerprints/reports; reject rollback/conflicts/invalid quantities; upgrade legacy snapshots/projections; concurrent reads with bounded protocol/lifetimes.

Open: mmap/SBE/FIX/CPU pinning, equal-time multicast, Raft/elections/sequencer replication, separate outbound event bus, L3 subscriptions and production/real settlement.

## 2026-11-05 replay and market-update integrity

- Compare canonical state checksums for promotion, replay/projection equality and current/legacy snapshot compatibility. Equal-valued JSON booleans, floats and integers are distinct; dictionary insertion order is irrelevant.
- Audit monetary values, quantities and sequence counters as strict integers before further matching; even an equal-valued float projection drift blocks new commands until replay rebuilds the state.
- Retained orders keep their original identity and intent. Only remaining/filled/status/reason may change, with nondecreasing fills, nonincreasing remaining quantities and immutable terminal orders; a newly published order must originate in the current batch sequence.
- Reject conflicting updates before committing the market cursor, public changes, duplicate fingerprint or private reports; retain the previous projection on failure. These checks do not add sequencer redundancy or automatic election.
