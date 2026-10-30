# Notification System

<!-- LEARNING_GUIDE_START -->
## Code reading guide

[Learn through a concrete example: architecture, state changes and annotated source (Chinese)](docs/learning-guide_cn.md).
<!-- LEARNING_GUIDE_END -->

A Python 3.9+ asyncio reference implementation of chapter 11 of `System Design Interview.md`. It supports iOS push, Android push, SMS and email through a pluggable `Provider` interface. The bundled provider **simulates delivery**; it does not contact APNs, FCM, SMS or email services.

[中文说明](README_CN.md) · [Design](docs/design.md) · [Chapter coverage](docs/chapter11-review.md)

## Installation

```powershell
python -m pip install -e ".[dev]"
```

There are no third-party runtime dependencies.

## Recoverable pipeline example

Run from this project directory after installation:

```python
import asyncio
from notification_system import (
    Channel, ContactInfo, NotificationLog, NotificationRequest,
    NotificationService, NotificationStatus, SimulatedProvider,
    SQLiteMessageQueue, SQLiteNotificationLog,
)

async def main():
    path = "notifications.sqlite3"
    service = NotificationService(queue=SQLiteMessageQueue(path))
    service.contacts.set(ContactInfo("user_1", email="user@example.com"))
    audit = NotificationLog(SQLiteNotificationLog(path))
    pool = service.create_worker_pool(
        {Channel.EMAIL: SimulatedProvider(Channel.EMAIL)}, audit,
        delivery_timeout=10.0,
    )
    await pool.start()
    try:
        notification_id = await service.send(NotificationRequest(
            "user_1", Channel.EMAIL, title="Welcome", body="Hello!",
            event_id="signup:user_1",  # stable identifier from the producer
        ))
        async def wait_for_acceptance():
            while service.tracker.get_current_status(notification_id) != NotificationStatus.SENT:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_for_acceptance(), 5.0)
        print(notification_id, audit.query(notification_id=notification_id))
    finally:
        await pool.stop()

asyncio.run(main())
```

`send()` accepts and queues work. Workers must be started separately. `SENT` means the provider accepted the request; delivery to the device and user clicks require callbacks. Replaying this example with the same database and `event_id` raises `DuplicateNotificationError` containing the original notification ID.

Use `NotificationService()` for a bounded in-memory queue. Its tasks and deduplication ledger disappear when the process exits.

## Implemented behavior

- Validate request types, basic email/phone formats, content size and registered endpoints. Resolve templates once and snapshot rendered content, metadata and endpoints before acceptance.
- Keep separate channel queues and configurable worker counts. Each worker sends sequentially to all registered device endpoints; other workers run concurrently. Register the platform with `contacts.add_device_token(user, token, Channel.IOS_PUSH)` or `Channel.ANDROID_PUSH`. Legacy untyped tokens remain eligible for either push channel.
- Check preferences at acceptance and immediately before provider calls, including retries. An opt-out can suppress pending work; it cannot retract a provider call already made.
- Apply per-user per-channel limits and a per-user cross-channel `global_rate_limit`. The latter is not a system-wide or cross-process quota. Rejected enqueue attempts do not consume quota or content deduplication entries.
- Deduplicate explicit `event_id` by `(event_id, recipient_id, channel)`. The SQLite ledger is committed atomically with the job and acceptance audit row, survives completion/restart, and supports concurrent queue instances. First acceptance wins; even a changed replay payload raises the original ID. Distinct events may contain identical content. Without `event_id`, content-hash TTL suppression is local to one service instance.
- Preserve queue capacity reservations while workers own tasks. Delay retries in the queue with exponential backoff and jitter, rather than sleeping in the worker. Provider exceptions and timeouts enter this retry path. Temporary queue-claim errors use interruptible backoff.
- Retry failed devices without resending successful devices on normal retry. Permanently rejected devices are tracked separately; temporary failures continue retrying and the final aggregate result remains `FAILED` if any device was permanently rejected.
- Recover SQLite jobs with renewable, fenced leases. Cancellation returns owned jobs; expired leases recover work after process death. Scheduled jobs use persisted `available` timestamps and do not block ready jobs.
- Retain acceptance, attempt and terminal audit history with query/retention APIs. `tracker` is an in-memory state machine; its event history is not a durable delivery ledger.

## Authenticated HTTP demo

```powershell
$env:NOTIFICATION_API_KEY = 'local-demo-key'
python examples/demo_server.py --database notifications.sqlite3 --port 8000
```

The server binds to `127.0.0.1`. It starts a live asyncio worker pool on its own event-loop thread and uses simulated providers. Every endpoint requires `Authorization: Bearer local-demo-key`; JSON bodies must be objects, at most 1 MiB, with `Content-Length`.

```powershell
$headers = @{ Authorization = 'Bearer local-demo-key' }
$body = @{recipient_id='user_1'; channel='email'; body='Hello'; event_id='demo-001'} | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/notifications -Method Post -Headers $headers -Body $body
```

| Endpoint | Behavior |
|---|---|
| `POST /notifications` | Accept work (`202`), optional `event_id` and timezone-aware ISO `scheduled_at` |
| `GET /notifications/<id>` | Live events and persisted audit history when SQLite is enabled |
| `GET /analytics` | Live final-state counters and per-channel pending/in-flight counts |
| `GET/PUT /settings/<user>` | Read/update channel preferences |
| `GET/POST /templates` | List/register templates |
| `POST /events` | Record verified `delivered`/`clicked` callbacks in live history |

A shared bearer key demonstrates verified internal callers. Production deployment requires TLS, client-specific authorization, provider-specific signed callbacks and secret rotation. Callback reconstruction after restart is not implemented.

## Limits and operations

SQLite supplies a local durable queue, not a replicated broker. It counts scheduled and leased jobs toward channel capacity; `depth()` reports waiting/scheduled/expired-lease jobs and `in_flight()` reports live leases. Monitoring queries are synchronous snapshots. SQLite mutations and audit writes run in worker threads; it serializes writers and is not proof of million-notification daily capacity.

The delivery guarantee is **at least once**. A provider can accept a request before the process dies and before local acknowledgement, causing replay. Use a stable per-endpoint idempotency key in a real provider adapter. `event_id` prevents producer re-acceptance, not duplicate provider side effects during crash recovery. Completed/rejected endpoint progress is persisted when a job is rescheduled; a crash before that checkpoint can replay an endpoint.

Contacts, preferences, templates, counters and content TTL deduplication are memory-resident. Rehydrate them before starting recovered workers. Shared durable metadata, cross-process frequency caps, metadata cache invalidation, real provider adapters, external alerting, broker replication and autoscaling remain deployment work. The demo uses one HTTP request thread; concurrency is in the worker pipeline.

The durable event ledger is intentionally retained after audit cleanup. Plan its retention separately before using unbounded event IDs. Queue payloads and logs contain contact/content data and need appropriate local file access and retention.

## Verification

```powershell
python -m pytest tests -q
```

See [chapter review](docs/chapter11-review.md) for coverage, regression evidence and remaining deployment boundaries.
