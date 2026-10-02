# Notification System

An asyncio-based notification delivery library implementing an event-driven pipeline with multi-channel support, rate limiting, deduplication, retry with exponential backoff, and full lifecycle event tracking.

**Zero runtime dependencies** — built entirely on the Python standard library.

## Installation

```bash
# Install in development mode with test dependencies
pip install -e .[dev]
```

Requires Python >= 3.9.

## Quick Start

```python
import asyncio
from notification_system import (
    NotificationService,
    NotificationRequest,
    Channel,
    ContactInfo,
)

async def main():
    # Create the service with default configuration
    service = NotificationService()

    # Register contact info for the recipient
    service.contacts.set(ContactInfo(
        user_id="user_123",
        email="user@example.com",
        phone="+1234567890",
        device_tokens=["token_abc"],
    ))

    # Send a notification
    request = NotificationRequest(
        recipient_id="user_123",
        channel=Channel.EMAIL,
        title="Welcome!",
        body="Thanks for signing up.",
    )
    notification_id = await service.send(request)
    print(f"Sent: {notification_id}")

    # Check status
    status = service.tracker.get_current_status(notification_id)
    print(f"Status: {status}")

asyncio.run(main())
```

## API Overview

### Core Classes

| Class | Description |
|-------|-------------|
| `NotificationService` | Central orchestrator — validate → preferences → rate limit → dedup → enqueue |
| `NotificationConfig` | System configuration (queue depth, workers, rate limits, retry, dedup) |
| `NotificationRequest` | Immutable request dataclass (recipient, channel, title, body, template) |
| `Channel` | Enum: `IOS_PUSH`, `ANDROID_PUSH`, `SMS`, `EMAIL` |
| `NotificationStatus` | Lifecycle states: CREATED → QUEUED → SENDING → SENT → DELIVERED → CLICKED |

### Pipeline Components

| Class | Description |
|-------|-------------|
| `MessageQueue` | Per-channel asyncio.Queue with configurable max depth |
| `WorkerPool` | Async worker tasks consuming from queues and invoking providers |
| `SlidingWindowRateLimiter` | Per-user, per-channel and global rate limiting |
| `Deduplicator` | Content-hash based dedup with configurable TTL |
| `RetryHandler` | Exponential backoff + jitter for failed deliveries |

### Supporting Components

| Class | Description |
|-------|-------------|
| `Provider` / `SimulatedProvider` | Abstract delivery interface + test implementation |
| `NotificationTemplate` / `TemplateRegistry` | `{placeholder}` substitution templates |
| `NotificationSettings` | Per-user, per-channel opt-in/opt-out preferences |
| `ContactInfoStore` | User contact info (email, phone, device tokens) |
| `EventTracker` | State machine lifecycle tracking + analytics |
| `NotificationLog` / `InMemoryNotificationLog` | Persistent notification attempt logging |

### Exceptions

All exceptions inherit from `NotificationError`:

- `ValidationError` — invalid request (missing contact, opted out, bad template)
- `QueueFullError` — channel queue at max capacity
- `RateLimitExceededError` — rate limit exceeded for recipient
- `DuplicateNotificationError` — duplicate content suppressed
- `TemplateRenderError` — template missing required placeholders
- `DeliveryError` — provider delivery failure

## Configuration

```python
from notification_system import NotificationConfig, RateLimitConfig, RetryConfig, Channel

config = NotificationConfig(
    # Queue settings
    queue_max_depth=10000,
    workers_per_channel=3,

    # Per-channel rate limits
    per_channel_rate_limits={
        Channel.EMAIL: RateLimitConfig(max_count=20, window_seconds=3600.0),
        Channel.SMS: RateLimitConfig(max_count=10, window_seconds=3600.0),
        Channel.IOS_PUSH: RateLimitConfig(max_count=50, window_seconds=3600.0),
        Channel.ANDROID_PUSH: RateLimitConfig(max_count=50, window_seconds=3600.0),
    },
    global_rate_limit=RateLimitConfig(max_count=100, window_seconds=3600.0),

    # Deduplication window
    dedup_window_seconds=300.0,

    # Retry settings
    retry=RetryConfig(
        max_retries=3,
        base_delay=1.0,
        max_delay=300.0,
        jitter_factor=0.1,
    ),

    # Log retention
    log_retention_seconds=86400.0 * 30,  # 30 days
)

service = NotificationService(config=config)
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    Public API (NotificationService)                       │
│         validate → preferences → rate limit → dedup → enqueue            │
├─────────────────────────────────────────────────────────────────────────┤
│  Message Queues (per-channel)     │  Worker Pool (per-channel)           │
│  asyncio.Queue × 4 channels      │  asyncio.Task workers × N            │
├───────────────────────────────────┼──────────────────────────────────────┤
│  Rate Limiter          │  Deduplicator         │  Retry Handler          │
│  (sliding window)      │  (content-hash TTL)   │  (exp backoff + jitter) │
├────────────────────────┼───────────────────────┼─────────────────────────┤
│  Provider Interface (ABC)         │  Simulated Provider (testing)        │
│  APNs / FCM / SMS / Email         │  configurable latency + failure      │
├─────────────────────────────────────────────────────────────────────────┤
│  Templates       │  Settings       │  Contact Info    │  Event Tracker   │
│  (placeholder)   │  (opt-in/out)   │  (email/phone/   │  (state machine) │
│                  │                 │   device tokens)  │                  │
└─────────────────────────────────────────────────────────────────────────┘
```

### Pipeline Flow

1. **Validate** — Verify recipient has contact info for the channel
2. **Preferences** — Check user hasn't opted out of the channel
3. **Rate Limit** — Enforce per-channel and global frequency caps
4. **Dedup** — Suppress duplicate content within the TTL window
5. **Enqueue** — Place task in the per-channel asyncio.Queue
6. **Worker Delivery** — Workers pull tasks, invoke providers, handle retries

## Using Templates

```python
import asyncio
from notification_system import (
    NotificationService,
    NotificationRequest,
    NotificationTemplate,
    Channel,
    ContactInfo,
)

async def main():
    service = NotificationService()

    # Register a template
    template = NotificationTemplate(
        template_id="order_shipped",
        title="Order {order_id} Shipped",
        body="Hi {name}, your order is on its way!",
    )
    service.templates.register(template)

    # Register contact info
    service.contacts.set(ContactInfo(user_id="user_1", email="a@b.com"))

    # Send using template
    request = NotificationRequest(
        recipient_id="user_1",
        channel=Channel.EMAIL,
        template_id="order_shipped",
        template_params={"order_id": "ORD-42", "name": "Alice"},
    )
    nid = await service.send(request)
    print(f"Sent: {nid}")

asyncio.run(main())
```

## Running with Worker Pool

```python
import asyncio
from notification_system import (
    NotificationService,
    NotificationConfig,
    NotificationRequest,
    NotificationLog,
    InMemoryNotificationLog,
    Channel,
    ContactInfo,
    SimulatedProvider,
    WorkerPool,
    RetryHandler,
)

async def main():
    config = NotificationConfig(workers_per_channel=2)
    service = NotificationService(config=config)

    # Set up contact info
    service.contacts.set(ContactInfo(
        user_id="user_1",
        email="user@example.com",
        device_tokens=["token_1"],
    ))

    # Create providers for each channel
    providers = {
        channel: SimulatedProvider(channel=channel, latency_ms=20.0)
        for channel in Channel
    }

    # Create retry handler and worker pool
    log = NotificationLog(backend=InMemoryNotificationLog())
    retry_handler = RetryHandler(
        config=config.retry,
        queue=service.queue,
        tracker=service.tracker,
        notification_log=log,
    )
    pool = WorkerPool(
        queue=service.queue,
        providers=providers,
        tracker=service.tracker,
        retry_handler=retry_handler,
        workers_per_channel=config.workers_per_channel,
    )

    # Start workers
    await pool.start()

    # Send notifications
    nid = await service.send(NotificationRequest(
        recipient_id="user_1",
        channel=Channel.EMAIL,
        title="Hello",
        body="World",
    ))
    print(f"Queued: {nid}")

    # Allow time for delivery
    await asyncio.sleep(0.5)

    # Check final status
    status = service.tracker.get_current_status(nid)
    print(f"Final status: {status}")

    # Graceful shutdown
    await pool.stop()

asyncio.run(main())
```

## Running Tests

```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Run a specific test file
pytest tests/test_service.py

# Run property-based tests only
pytest tests/test_properties.py

# Run with coverage (if pytest-cov installed)
pytest --cov=notification_system
```

## Running the Demo Server

```bash
# Start the HTTP demo server (default port 8080)
python examples/demo_server.py

# Available endpoints:
# POST /notifications         - Send a notification
# GET  /notifications/<id>    - Get notification status
# GET  /analytics             - Get aggregate analytics
# GET  /settings/<user_id>    - Get user preferences
# PUT  /settings/<user_id>    - Update user preferences
# POST /templates             - Register a template
# GET  /templates             - List templates
```

## Project Structure

```
notification-system/
├── pyproject.toml
├── README.md
├── src/notification_system/
│   ├── __init__.py          # Public API exports
│   ├── config.py            # NotificationConfig dataclass
│   ├── models.py            # Data models and enums
│   ├── exceptions.py        # Custom exception hierarchy
│   ├── service.py           # NotificationService orchestrator
│   ├── queue.py             # Per-channel asyncio.Queue
│   ├── worker.py            # WorkerPool and Worker
│   ├── provider.py          # Provider ABC + SimulatedProvider
│   ├── templates.py         # Template placeholder substitution
│   ├── rate_limiter.py      # Sliding window rate limiter
│   ├── dedup.py             # Content-hash deduplicator
│   ├── retry.py             # Exponential backoff + jitter
│   ├── settings.py          # Per-user opt-in/opt-out
│   ├── contacts.py          # Contact info store
│   ├── tracker.py           # Event lifecycle tracker
│   └── log.py               # Notification log
├── tests/
├── examples/
│   └── demo_server.py       # HTTP demo server
└── docs/
```

## License

MIT
