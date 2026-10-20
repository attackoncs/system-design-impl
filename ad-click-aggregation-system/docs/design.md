# Ad Click Event Aggregation: reference design

## Process and persistence boundaries

The runnable lab has six independent processes: one ingress/raw-log HTTP service, four partition aggregation HTTP services, and one query/reduce HTTP service. The query and workers never open the ingress database. Each worker owns a different SQLite database and pulls pages over HTTP. No in-memory queue or shared Python object substitutes for transport.

Ingress stores normalized immutable events and partition sequence heads in one SQLite WAL transaction with `synchronous=FULL`. `event_id` is a global unique key. Reusing an ID with the same normalized payload returns its original position; a different payload returns 409 and rolls back the entire batch. IDs and raw data are retained without automatic expiry, so deduplication survives all supported replays. The default raw-log limit is one million unique events; new writes above it return 429, while exact duplicates still succeed. A batch contains 1–200 events. A log page contains at most 200.

This deliberately uses a single durable log owner. It is a transport reference, not a replicated Kafka replacement. It does not reuse chapter 20's wire protocol: the small source protocol exposes immutable, independently replayable partition sequences and lineage. Replacing this source with a replicated queue requires preserving those invariants and the local atomic-checkpoint contract. No replicated broker or coordinator failover claim is made here.

## Event identity and hotspot partitioning

Events require exactly `event_id`, `ad_id`, `click_timestamp`, `user_id`, `ip`, `country`. Timestamps are integer epoch seconds, at most 30 seconds ahead of ingress time. Country is normalized to an uppercase two-letter code; IP uses Python's IP address canonicalization. String dimensions are bounded to 128 characters. Unknown fields and malformed data fail the whole batch before append.

`partition = first_64_bits(SHA256(canonical_JSON(event_id))) % partition_count`. Salting on event identity spreads even one hot ad across workers. The partition count is persisted in the log and cannot change in place. The worker persists its assigned partition; changing queue lineage fails closed. Scaling requires draining/replaying to a new log and result generation, then explicitly changing deployment configuration; there is no automatic online reassignment or rebalance.

## Exactly-once effects and recovery

Transport is at least once: workers request the page following their durable local checkpoint. A single worker transaction validates lineage, offsets and SHA256 payload checksums, inserts dedup keys, increments dimension buckets, and advances its checkpoint. A crash before commit rolls back both counts and checkpoint. After commit the next pull resumes from the saved position; replaying old positions is a no-op. Extra event deduplication within each worker also protects repeated IDs at later positions in a compatible source.

There is no separate queue ACK that could get ahead of output persistence. On malformed/corrupt/gapped source data workers retain the failing offset and expose an error; they do not silently skip billing-relevant events. Rebuild uses a new explicitly named database and captures a finite raw-log head. CLI output paths are exclusively reserved to avoid overwriting existing results. Promotion of a rebuilt generation is an operator action, not an automatic replacement of running stores.

The JSONL tailer checkpoints byte offset plus SHA256 of the consumed file prefix after receiving the ingress ACK. A lost ACK or missing local checkpoint replays stable IDs safely. Incomplete final lines wait for a newline. Mutated/truncated prefixes fail closed; rotation requires a new checkpoint. Tailer checkpoints have one owner; no concurrent tailers may share one checkpoint file. Historical timestamps remain replayable because ingestion time has no lower event-time cutoff.

## Event time, watermark, and correction semantics

Buckets use `floor(click_timestamp / 60) * 60`, and all queries use minute-aligned `[from,to)` ranges. Every range is at most 1440 minutes. Partial-minute sliding windows are intentionally rejected. Default query ends at the current minute boundary, so it covers the last complete minute.

Per-partition watermark is monotone: after a record it is the maximum of its prior value, maximum seen event time minus allowed lateness, and recorded ingress acceptance time minus allowed lateness. An idle, caught-up worker also advances it from wall clock minus lateness. Default allowance is 120 seconds. Watermarks therefore assume reasonably synchronized clocks; restart does not move them backwards. An event for a bucket whose end is already at/below the prior watermark increments the late-correction counter.

All valid late events are still counted, including events older than the allowance. `closed` means the requested end is at/below every partition watermark; it does not promise an immutable result or that the queue has no backlog. A late event increments that bucket's revision and the partition checkpoint. Responses carry checkpoint vectors, current lag estimate, watermark, `closed`, and `late_corrections_allowed=true`. The late counter is an operational observation affected by idle wall-clock advancement, not a deterministic billing result of offline replay.

## Filtering, exact Top N, and consistency

One bucket row contains `(minute, ad_id, user_id, ip, country, count, revision)` with a composite primary key. Secondary indexes cover advertisement, country, user and IP by minute. Filters are exact AND conditions; IPv6 spelling variants canonicalize to the same value. No dimension cube of every filter combination is materialized, but high-cardinality dimensions can still produce nearly one row per event. This trades storage for arbitrary supported dimension intersections.

Each worker sums its buckets into **all** matching advertisement totals, up to 10,000 candidate ads. The query process fetches partitions concurrently, sums each ad across them, then sorts by descending count and ascending ad ID for deterministic ties. It also enforces a global 10,000-candidate limit and a requested Top N in 1–1000. Budget errors return 422, not an approximate/truncated answer. Local top-N truncation is incorrect with salted hot keys: hot=6 on each of four shards wins globally with 24 even when each shard's local winner has 9. A real network test covers this counterexample.

Count and checkpoint come from one read transaction per worker. The overall response is a vector of such snapshots, not an atomic snapshot across all partitions. Lag compares a source-head snapshot taken before worker reads and is floored at zero if a worker advances past it. Lag=0 is not a linearizable freshness guarantee. Missing/errored workers, missing configured partitions, and inconsistent lineage return 503. The system never reports partial Top N as a successful query.

## Replay and independent reconciliation

`rebuild` feeds the original event stream into the same aggregation implementation in a separate database (Kappa style). `reconcile` independently reads each worker's audit hash and its checkpoint in one transaction, then batch-counts raw events only through that position. It hashes sorted full dimension rows, not only total clicks, so allocation errors with equal grand totals can be detected. Audit snapshots may differ in time across partitions, but each comparison is exactly bounded by its own source position. Comparison does not rewrite online data; an inconsistency is reported with exit status 1 by the CLI. The current batch/audit implementation loads rows into memory and is an administrative small-scale tool, not a distributed billion-event batch engine.

## Security and operational limits

Four distinct bearer credentials separate write, read, internal/raw-data and admin capabilities. Missing or short credentials prevent startup. Raw logs and user/IP dimension rows are available only to internal/admin roles; public health contains no secrets. Requests are bounded to 1 MiB and HTTP clients to 8 MiB responses. Localhost is the default binding. This server has no TLS, tenant ACLs, encryption at rest, fraud prevention, automated privacy retention or distributed rate limiting; a production gateway and a privacy/data-lifecycle design are required before internet deployment.

Worker status exposes persistent position, bucket count, watermark, correction count, last poll and current error. Ingress exposes capacity and partition heads. These support backlog and reconciliation monitoring; no third-party monitoring provider is claimed. SQLite and raw log remain single-machine failure domains, HTTP workers are statically configured, and there is no automatic worker/storage failover. Tests cover real process exits and restart on retained local disks, not host/disk loss or replicated consensus. Original book throughput and availability targets remain unverified.
