# Notification system design

This document describes the executed reference pipeline after the chapter 11 review. [Chapter coverage](chapter11-review.md) identifies the deployment features that remain outside it.

## Components and ownership

`NotificationService` validates and renders requests. It owns recipient-striped admission locks, preferences, contacts, template registry, in-memory frequency windows and fallback content deduplication. It uses the injected queue rather than sending directly. `create_worker_pool()` connects that same queue, preferences and tracker to `RetryHandler` and `WorkerPool`, so opt-outs are rechecked before delivery.

`MessageQueue` stores per-channel heaps ordered by availability. Low-level `dequeue()` removes a task; recoverable consumers must use `claim()/ack()/reschedule()`. Worker claims reserve capacity until a terminal acknowledgement or atomic transfer to a delayed queue item. Released receipts are rejected to prevent compensation from resurrecting completed jobs.

`SQLiteMessageQueue` implements the same worker contract over `notification_jobs`. Database mutations run through `asyncio.to_thread`, with per-instance serialization and SQLite `BEGIN IMMEDIATE` transactions across instances. The file uses WAL and synchronous FULL. A queue has no background connection or scheduler task to close: each operation closes its connection. Depth methods use short synchronous snapshot queries.

`Provider.deliver(task)` receives rendered request data and `task.endpoint`. One worker sends endpoints sequentially with a configurable per-call timeout; concurrent workers provide bounded delivery parallelism. The bundled implementation is simulated. Real adapters must map APNs/FCM/SMS/email responses into `DeliveryResult` and respect cancellation.

## Acceptance transaction

1. Validate types and timezone-aware schedule values. Under a recipient stripe, look up an explicit business event ID if supplied.
2. Resolve registered endpoints and check preferences. Validate basic email/phone syntax, cap endpoint count at 100 and rendered title/body at 1 MiB.
3. Resolve templates once and copy all mutable request dictionaries. Platform mappings filter registered push tokens; legacy untyped tokens remain compatible.
4. Check per-user channel and per-user cross-channel quota. With no event ID, check local content TTL deduplication using an unambiguous serialized title/body pair.
5. Record local CREATED/QUEUED before making the task visible to workers. Accept into the queue; record quota and content deduplication only after success. Rejections record local FAILED and do not consume those reservations.
6. SQLite acceptance atomically inserts the job, QUEUED audit entry, and optional unique `(event_id, recipient_id, channel)` ledger row. Capacity counts all queued and claimed rows. A duplicate returns the original notification ID through `DuplicateNotificationError`, including after terminal acknowledgement. First acceptance wins if replay content differs.

Local recipient locks protect same-user checks across channels in one service. Database event uniqueness protects queue instances independently of those locks. Quotas and metadata are not shared across processes. Cancellation during acceptance can leave an accepted row despite an uncertain caller response; producers should replay a stable event ID.

## Worker lifecycle and retries

Workers atomically claim ready rows using random receipt tokens. A claimed SQLite row remains until terminal acknowledgement. A heartbeat extends the lease; claims with expired leases can be taken by another worker. Renew, reschedule and acknowledgement require the current token and a still-live lease, fencing stale owners. Database claim cancellation waits for the transaction result and returns a completed claim. Worker cancellation also returns a claim that completed just before its awaiting parent was cancelled.

The pool serializes start/stop, rejects duplicate start, and cleans up a partially started pool. Graceful stop completes current deliveries, without draining all queued/delayed jobs. Immediate stop cancels and awaits workers, which return their jobs. Provider timeouts/exceptions become delivery failures; queue-claim exceptions use interruptible bounded backoff rather than killing a worker or busy-looping.

Retries increase the attempt counter and atomically replace the owned row with a future availability timestamp. Workers pass `defer=True` to `RetryHandler`, so another ready notification can use the released worker. The public direct handler retains its legacy await-the-delay behavior for component callers. Configuration rejects invalid numeric types and non-finite values; backoff exponentiation is capped before large integers grow.

Per-device successes and permanent rejections are separate task fields. A normal retry sends only remaining temporary failures. Mixed permanent/transient failure continues the transient work, but final aggregate status stays FAILED if any endpoint was rejected. Progress is persisted at rescheduling; a crash before that checkpoint can replay a previously contacted endpoint.

## Auditing, scheduling and APIs

SQLite terminal acknowledgement deletes the pending job and writes the terminal audit row in one transaction. `SQLiteNotificationLog` supports filtered audit queries and retention cleanup; attempts are audit facts and may include FAILED attempts before a later success, while `EventTracker` models the live notification lifecycle (`SENDING -> QUEUED` for retry).

The HTTP demo owns a continuously running event-loop thread and submits every operation to it. It starts workers and rejects unauthenticated requests using a required bearer key, checks JSON object/body size, and returns 202 for accepted notifications. It uses the same SQLite file for durable jobs/logs when `--database` is given. User preferences and template changes run on the owning event loop. Delivered/clicked callbacks update live state; callback-state recovery after restart is pending.

`send(request, scheduled_at=aware_datetime)` persists the due timestamp. Polling claims select ready tasks instead of sleeping behind the earliest future task. `depth()` counts pending/scheduled/reclaimable jobs, and `in_flight()` counts current leases. The demo analytics route exposes these values and live status counts.

## Guarantees and deployment work

Accepted SQLite jobs survive a process exit and are retried after lease expiry. This is at-least-once delivery: an external acknowledgement cannot be atomic with a local database transaction. Producer event deduplication prevents duplicate acceptance, while provider-side per-endpoint idempotency is needed for external side effects.

SQLite is a local, serialized-writer backend. There is no replicated broker, horizontal autoscaler, shared metadata database/cache, cross-process limiter or real notification provider integration. Contacts, templates, settings and live tracker history must be rehydrated before recovered workers start. External failure alert delivery, signed vendor callbacks, authorization by client, and TLS are deployment integrations. Persistent event ledger retention is separate from audit cleanup. No production throughput target is claimed.
