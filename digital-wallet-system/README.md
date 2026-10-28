# Digital Wallet — Chapter 28

An executable reference for wallet-to-wallet transfers with integer USD cents, account sharding, TCC recovery, deterministic event replay and historical balances. **All opening balances are synthetic; no bank, card or real money service is connected.**

The loopback lab uses seven independent processes: one fixed coordinator, one worker, two account shards and three coordinator-journal replicas. A journal entry needs two of three HTTP replicas before it is committed. The account shards are independent SQLite databases and are **not replicated**. This is a fixed-writer replication protocol with manual epoch fencing, not Raft, automatic leader election or production HA.

## Quick start

Python 3.10+, standard library only. From this directory in PowerShell:

```powershell
$env:PYTHONPATH = 'src'
New-Item -ItemType Directory -Force .runtime/tmp | Out-Null
$env:TEMP = (Resolve-Path .runtime/tmp).Path
$env:TMP = $env:TEMP
python -m unittest discover -s tests -v
python examples/network_demo.py
python tools/benchmark.py --transfers 20 --clients 4
```

The 54 tests include seven real HTTP process tests. The demo pauses after source confirmation, checks in-transit funds, restarts the coordinator using its original database and completes the transfer exactly once. It also executes a local transfer. Labs create private temporary configurations and stop only their own processes; temporary databases are removed afterward. The benchmark checks final balances and reconciliation, not just response codes.

For a persistent lab, run `python tools/init_config.py` once. It creates `.runtime/private-config.json` without overwriting existing files. Set `PYTHONPATH=src` in seven separate terminals and run:

```powershell
python -m digital_wallet.cli replica --index 0 --config .runtime/private-config.json
python -m digital_wallet.cli replica --index 1 --config .runtime/private-config.json
python -m digital_wallet.cli replica --index 2 --config .runtime/private-config.json
python -m digital_wallet.cli shard --index 0 --config .runtime/private-config.json
python -m digital_wallet.cli shard --index 1 --config .runtime/private-config.json
python -m digital_wallet.cli coordinator --config .runtime/private-config.json
python -m digital_wallet.cli worker --config .runtime/private-config.json
```

The coordinator defaults to port 9880, shards 9881–9882, journal replicas 9883–9885. Inspect generated `accounts` for names, ownership and synthetic opening balances. Keep generated credentials and databases private; the generator does not change filesystem permissions. `.runtime/` is Git-ignored. Configuration and binding are restricted to loopback HTTP.

## Transfer and balance API

Use `Authorization: Bearer <owners.alice token>` for Alice's accounts, or the separate Bob token for Bob's. Admin and inter-service credentials are distinct. Only an owner may debit an account; both source and destination owners can inspect their transfer. Account balance/history is visible only to its owner.

`POST /v1/wallet/balance_transfer` accepts exactly:

```json
{
  "transaction_id": "transfer_1",
  "from_account": "<owned source from config>",
  "to_account": "<destination from config>",
  "amount": 500,
  "currency": "USD"
}
```

The amount is a positive integer in **USD cents**, not a decimal string or float. Booleans are rejected. Identifiers accept 1–80 ASCII letters, digits, underscores or hyphens. The same transaction ID and body return the existing transfer; changed content returns 409. Different IDs mean different instructions, so always retain the original ID after a timeout or 503. A 503 may have left a durable intent that will later commit.

Use `POST /v1/wallet/balance_transfer?wait_ms=2000` or `GET /v1/transfers/<transaction_id>?wait_ms=2000` for a bounded completion wait. `wait_ms` accepts integers 0–2000, default 0. A committed terminal fact notifies waiters; timeout returns the current `PENDING` without canceling money movement. At most eight waiters are admitted. Query using the original ID after a restart/disconnect; this is not a durable push channel.

Poll `GET /v1/transfers/<transaction_id>`. Acceptance returns `PENDING`, not successful money movement. Terminal results are `SUCCEEDED`, `CANCELED` or `REJECTED` (local insufficient funds). Cross-shard insufficient funds selects cancellation. Transport ambiguity leaves the transfer pending, with retry/dead details available to administrators.

`GET /v1/accounts/<account>` returns available/held cents and a shard-local event sequence. `?at=<sequence>` reconstructs that account from the shard event prefix. Sequence 1 is genesis. Sequences from different shards are not a shared transaction timestamp. These read projections expose TCC intermediate states and are not a globally atomic two-account read.

## Monetary and recovery semantics

- **Same shard:** one event and one transaction update both balances. Insufficient funds are durably rejected. Duplicate requests do not append another effect.
- **Cross shard:** source prepare moves available to held; destination prepare validates the account but credits nothing. After both prepare, COMMIT is durably replicated. Source confirm removes the hold, then destination confirm credits available. Completion is journaled last.
- **Cancellation:** a durable CANCEL decision releases source holds and installs destination/source tombstones. Late Try returns the canceled state. Confirm after cancel is rejected. No cancellation is allowed after COMMIT or for ambiguous local transfers.
- **Unknown response:** inspect participant state and resume the original command. Holds have no automatic expiry. Five consecutive participant failures mark recovery work dead for manual inspection, without reporting a financial failure or releasing funds.
- **Conservation:** wallet available + held + confirmed-source/uncredited-destination in-transit funds equals configured opening supply. The audit checks journal commands, participant effects and replayed projections under serialized coordinator access.

The coordinator serializes money mutations and worker steps; balance reads and completion waits run concurrently. Pending transfers progress round-robin. Three owned RPC workers replicate journal prefixes in parallel, under a shared append/sync lock; only the acknowledged snapshot is marked committed. Mutations require a reachable two-replica journal quorum and matching persisted shard epochs. A participant may commit before its response is lost; its stable transaction ID prevents another debit. A journal append may reach only one replica before failure; its original durable intent remains on coordinator disk and is replicated before later work. **Do not start a new authority with an empty or copied stale disk.** There is no recovery/promotion protocol for loss of that disk.

## Administration

Use the admin token:

| Method | Coordinator route | Purpose |
|---|---|---|
| GET | `/admin/status` | Pending transfers, retry errors/dead flags, journal count and configured epoch |
| GET | `/admin/audit` | Quorum check, participant/log projection reconciliation and supply including in-transit funds |
| POST | `/admin/sync` with `{}` | Catch replicas up from the retained authoritative journal |
| POST | `/admin/retry/<id>` with `{}` | Clear recovery backoff/dead flag, retaining original intent |
| POST | `/admin/cancel/<id>` with `{}` | Select cancellation only for an undecided cross-shard transfer |

Shard `POST /admin/snapshot` with `{}` writes an atomic JSON snapshot with checksum. Startup verifies the complete event chain and snapshot prefix, then replays the tail into the read projection. It does not re-execute transfer commands. This prioritizes integrity over snapshot startup speed; history is not compacted.

For **manual same-disk epoch recovery**, stop the worker/coordinator, fence all three journal replicas and both shards using `POST /admin/fence` with a strictly higher `epoch` and a chosen `leader` identifier, update both fields in the private config, then restart the coordinator on its original database and worker. Bring every participant to the new epoch before resuming. Old epochs and same-epoch different owners are rejected durably. Partial maintenance may block progress; this is not automatic failover or consensus election. Shared credentials/configuration and the single retained authority disk are trusted operational prerequisites.

Fault injection: shard `POST /admin/control` accepts `{"lose_reply":false,"reject_prepare":false}`. `lose_reply=true` returns 503 after a durable participant action; `reject_prepare=true` gives a definitive prepare rejection. Replica `/admin/control` accepts `{"reject_append":true}` while keeping status probes alive, to exercise minority-only journal acceptance. Reset flags before retrying.

## Evidence and limits

See the [chapter review and remaining gaps](docs/chapter28-review.md), [design](docs/design.md), [verification report](benchmarks/REPORT.md) and [current wait sample](benchmarks/review-wait-2026-10-27.json), [poll sample](benchmarks/review-poll-2026-10-27.json) and [historical sample](benchmarks/latest.json). Million TPS and 99.99% availability are unproven book targets. No cross-host durability, authority-disk loss recovery, automatic elections, replicated participant balances, bank integration, FX, deposits, withdrawals, fees, chargebacks or production identity/key management is implemented.

The reference bounds configuration to 100 accounts and accepted history to 1,000 transfers, retains all logs, incrementally caches committed coordinator facts in a rebuildable read model, and uses full shard state for audit. It needs pagination, indexed histories, streaming replication/replay, partition-map migration, stronger deployment/security and operational backup/recovery before larger workloads.

## Engineering limits in this review

HTTP admits 32 active connections, with 16 coordinator operation contexts and at most eight completion waiters. The total I/O budget is 15 seconds; each replica RPC is capped at 0.5 seconds, each shard RPC at 0.75 seconds, within the shared deadline. Incoming JSON is limited to 1 MiB and RPC/output to 8 MiB. Duplicate fields, non-finite numbers, repeated credentials, ambiguous framing, unknown/repeated queries and extra admin fields are rejected. Internal RPC bypasses environment proxies and does not follow redirects.

Commands are copied and validated before writer waits. Participant receipts must match the complete intent, role and valid state. SQLite read transactions pin balances, sequence and audit checks to a coherent snapshot. Current balance and individual participant replies avoid transferring complete history. Snapshot publication uses unique temporary files, an owned publication lock, fsync and atomic replacement, alongside ordinary money writes. Socket deadlines cannot force-stop SQL/CPU or arbitrary blocking adapters; accepted tasks retain ownership until they exit.

The retained authority binds account owners, opening balances and shard count. Ordinary restarts reject changes; legacy databases establish this binding on their first upgrade, so verify configuration against retained history before that upgrade. Epoch/leader still follow manual fencing. Shutdown closes the listener, cancels waits and drains owned replica RPCs; bind failures close coordinator resources too.
