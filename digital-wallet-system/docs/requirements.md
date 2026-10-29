# Chapter 28 Digital Wallet Requirements

Source: Chapter 28 of "System Design Interview.md" in the root directory. Only wallet-to-wallet transfers of synthetic balances are implemented; one million TPS and 99.99% availability are goals from the original book and cannot be derived from local testing.

- Amounts are integer USD cents; accounts and synthetic opening balances are statically provisioned, with no connection to real payment channels.
- Same-shard atomic transfers, insufficient-balance rejection, global transfer-ID idempotency and content conflict checking; concurrent debits do not overdraw.
- SHA256 account sharding; TCC is chosen for cross-shard transfers, retaining pending states, commit/cancel decisions, held and in-transit funds, with support for continuing after crashes.
- Append-only checksummed event logs, deterministic replay, independent read projections, per-shard sequence-based historical queries, and snapshot-plus-tail-event recovery.
- A fixed coordinator with a three-node HTTP journal majority interface and persistent epoch fencing; no claim of automatic leader election, account replica HA, or original-disk-loss recovery.
- Cancellation tombstones block late Try requests; cancellation is impossible after the COMMIT decision; unknown responses neither automatically release holds nor change the paying identity.
- Recovery by querying participant states, manual retry after consecutive failures, journal/projection/participant reconciliation, and fund conservation including in-transit amounts.
- Independent processes verify concurrency, network unavailability, minority acceptance, partial commits, restarts, and replay; bilingual documentation, demos, and measured reports are provided.

Explicitly excluded: deposits/withdrawals, FX, fees, chargebacks, real bank reconciliation, or production identity management. Per-shard history sequences are not global timestamps; TCC intermediate states are visible.

- Bounded completion notification: transfer POST/GET accept wait_ms=0..2000; waits release money locks, timeout retains state, and at most eight waiters are admitted.
- Freeze commands and validate receipts; coherent balance/sequence snapshots and concurrent safe snapshot publication; parallel replication with serialized append/sync and exact acknowledged-prefix commits.
- Bound connections/threads/output, share I/O deadlines, cancel/drain owned work, enforce JSON/HTTP contracts; rebuildable caches are not monetary truth.

Open: Raft/elections and participant event replicas, authority-disk-loss promotion, independently deployed CQRS readers, accelerated/compacted snapshots, million TPS and four-nines evidence.
