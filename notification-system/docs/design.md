# Design: Notification System

## Architecture Overview

The notification system library follows an event-driven pipeline architecture within a single process. The main orchestrator (`NotificationService`) validates requests, checks user preferences, applies rate limiting and deduplication, then dispatches notifications to per-channel async queues. Worker pools consume from these queues and deliver through pluggable provider interfaces. Failed deliveries are retried with exponential backoff. Every lifecycle event is tracked through a state machine for analytics and auditing.

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
├─────────────────────────────────────────────────────────────────────────┤
│  Models & Config & Exceptions & Notification Log                         │
└─────────────────────────────────────────────────────────────────────────┘
```

## Project Structure

```
notification-system/
├── pyproject.toml
├── README.md
├── docs/
│   ├── requirements.md
│   ├── design.md
│   └── tasks.md
├── src/
│   └── notification_system/
│       ├── __init__.py          # Public API exports
│       ├── config.py            # NotificationConfig dataclass
│       ├── models.py            # Data models (NotificationRequest, NotificationStatus, Channel, etc.)
│       ├── exceptions.py        # Custom exception hierarchy
│       ├── service.py           # NotificationService orchestrator
│       ├── queue.py             # MessageQueue with per-channel asyncio.Queue
│       ├── worker.py            # WorkerPool and Worker (asyncio tasks)
│       ├── provider.py          # Provider ABC + SimulatedProvider
│       ├── templates.py         # NotificationTemplate with placeholder substitution
│       ├── rate_limiter.py      # SlidingWindowRateLimiter
│       ├── dedup.py             # Deduplicator with content-hash TTL
│       ├── retry.py             # RetryHandler with exponential backoff + jitter
│       ├── settings.py          # NotificationSettings (per-user, per-channel opt-in/out)
│       ├── contacts.py          # ContactInfoStore (email, phone, device tokens)
│       ├── tracker.py           # EventTracker (state machine + analytics)
│       └── log.py               # NotificationLog (in-memory + ABC for backends)
├── tests/
│   ├── __init__.py
│   ├── test_models.py
│   ├── test_exceptions.py
│   ├── test_service.py
│   ├── test_queue.py
│   ├── test_worker.py
│   ├── test_provider.py
│   ├── test_templates.py
│   ├── test_rate_limiter.py
│   ├── test_dedup.py
│   ├── test_retry.py
│   ├── test_settings.py
│   ├── test_contacts.py
│   ├── test_tracker.py
│   ├── test_log.py
│   └── test_properties.py      # Property-based tests (Hypothesis)
└── examples/
    └── demo_server.py           # HTTP demo using stdlib http.server
```

## Component Design

### 1. Custom Exceptions (`exceptions.py`)

```python
class NotificationError(Exception):
    """Base exception for all notification system errors."""
    pass


class ValidationError(NotificationError):
    """Raised when a notification request is invalid."""

    def __init__(self, message: str, field: str = "") -> None:
        self.field = field
        super().__init__(message)


class QueueFullError(NotificationError):
    """Raised when a channel queue has reached maximum capacity."""

    def __init__(self, channel: str, max_depth: int) -> None:
        self.channel = channel
        self.max_depth = max_depth
        super().__init__(
            f"Queue for channel '{channel}' is full (max_depth={max_depth})"
        )


class RateLimitExceededError(NotificationError):
    """Raised when a notification exceeds the rate limit for a recipient."""

    def __init__(self, user_id: str, channel: str, limit: int, window: float) -> None:
        self.user_id = user_id
        self.channel = channel
        self.limit = limit
        self.window = window
        super().__init__(
            f"Rate limit exceeded for user '{user_id}' on channel '{channel}': "
            f"{limit} per {window}s"
        )


class DuplicateNotificationError(NotificationError):
    """Raised when a notification is suppressed as a duplicate."""

    def __init__(self, original_id: str) -> None:
        self.original_id = original_id
        super().__init__(
            f"Duplicate notification suppressed (original_id='{original_id}')"
        )


class TemplateRenderError(NotificationError):
    """Raised when a notification template cannot be rendered."""

    def __init__(self, template_id: str, missing_keys: list[str]) -> None:
        self.template_id = template_id
        self.missing_keys = missing_keys
        super().__init__(
            f"Template '{template_id}' missing required keys: {missing_keys}"
        )


class DeliveryError(NotificationError):
    """Raised when a provider fails to deliver a notification."""

    def __init__(
        self, notification_id: str, channel: str, reason: str, retryable: bool = True
    ) -> None:
        self.notification_id = notification_id
        self.channel = channel
        self.reason = reason
        self.retryable = retryable
        super().__init__(
            f"Delivery failed for '{notification_id}' on '{channel}': {reason}"
        )
```

### 2. Data Models (`models.py`)

```python
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Channel(Enum):
    """Notification delivery channels."""
    IOS_PUSH = "ios_push"
    ANDROID_PUSH = "android_push"
    SMS = "sms"
    EMAIL = "email"


class NotificationStatus(Enum):
    """Notification lifecycle states (state machine)."""
    CREATED = "created"
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    CLICKED = "clicked"
    UNSUBSCRIBED = "unsubscribed"


# Valid state transitions for the notification state machine
VALID_TRANSITIONS: dict[NotificationStatus, set[NotificationStatus]] = {
    NotificationStatus.CREATED: {NotificationStatus.QUEUED, NotificationStatus.FAILED, NotificationStatus.UNSUBSCRIBED},
    NotificationStatus.QUEUED: {NotificationStatus.SENDING, NotificationStatus.FAILED},
    NotificationStatus.SENDING: {NotificationStatus.SENT, NotificationStatus.FAILED},
    NotificationStatus.SENT: {NotificationStatus.DELIVERED, NotificationStatus.FAILED},
    NotificationStatus.DELIVERED: {NotificationStatus.CLICKED},
    NotificationStatus.FAILED: set(),
    NotificationStatus.CLICKED: set(),
    NotificationStatus.UNSUBSCRIBED: set(),
}


@dataclass(frozen=True)
class NotificationRequest:
    """A request to send a notification."""
    recipient_id: str
    channel: Channel
    title: str = ""
    body: str = ""
    template_id: Optional[str] = None
    template_params: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryResult:
    """Result from a provider delivery attempt."""
    success: bool
    provider_message_id: Optional[str] = None
    error: Optional[str] = None
    retryable: bool = True


@dataclass
class NotificationTask:
    """Internal task representation for queue processing."""
    notification_id: str
    request: NotificationRequest
    channel: Channel
    attempt: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    scheduled_at: Optional[datetime] = None


@dataclass(frozen=True)
class NotificationEvent:
    """A lifecycle event for a notification."""
    notification_id: str
    channel: Channel
    status: NotificationStatus
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    details: Optional[str] = None


@dataclass
class ContactInfo:
    """User contact information for notification delivery."""
    user_id: str
    email: Optional[str] = None
    phone: Optional[str] = None
    device_tokens: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LogEntry:
    """A persistent log entry for a notification attempt."""
    notification_id: str
    recipient_id: str
    channel: Channel
    content_summary: str
    timestamp: datetime
    status: NotificationStatus
    attempt: int = 1
    error_details: Optional[str] = None
```

### 3. Configuration (`config.py`)

```python
from dataclasses import dataclass, field
from typing import Optional

from notification_system.models import Channel


@dataclass
class RateLimitConfig:
    """Rate limit configuration for a single scope."""
    max_count: int = 100
    window_seconds: float = 3600.0


@dataclass
class RetryConfig:
    """Retry configuration for failed deliveries."""
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 300.0
    jitter_factor: float = 0.1


@dataclass
class NotificationConfig:
    """Configuration for the notification system.

    All numeric values must be positive. Raises ValueError on invalid input.
    """
    # Queue settings
    queue_max_depth: int = 10000

    # Worker settings
    workers_per_channel: int = 3

    # Rate limiting
    per_channel_rate_limits: dict[Channel, RateLimitConfig] = field(
        default_factory=lambda: {
            Channel.IOS_PUSH: RateLimitConfig(max_count=50, window_seconds=3600.0),
            Channel.ANDROID_PUSH: RateLimitConfig(max_count=50, window_seconds=3600.0),
            Channel.SMS: RateLimitConfig(max_count=10, window_seconds=3600.0),
            Channel.EMAIL: RateLimitConfig(max_count=20, window_seconds=3600.0),
        }
    )
    global_rate_limit: RateLimitConfig = field(
        default_factory=lambda: RateLimitConfig(max_count=100, window_seconds=3600.0)
    )

    # Deduplication
    dedup_window_seconds: float = 300.0

    # Retry
    retry: RetryConfig = field(default_factory=RetryConfig)

    # Log retention
    log_retention_seconds: float = 86400.0 * 30  # 30 days

    def __post_init__(self) -> None:
        """Validate configuration values."""
        if self.queue_max_depth <= 0:
            raise ValueError(f"queue_max_depth must be positive, got {self.queue_max_depth}")
        if self.workers_per_channel <= 0:
            raise ValueError(f"workers_per_channel must be positive, got {self.workers_per_channel}")
        if self.dedup_window_seconds <= 0:
            raise ValueError(f"dedup_window_seconds must be positive, got {self.dedup_window_seconds}")
        if self.retry.max_retries < 0:
            raise ValueError(f"max_retries must be non-negative, got {self.retry.max_retries}")
        if self.retry.base_delay <= 0:
            raise ValueError(f"base_delay must be positive, got {self.retry.base_delay}")
        if self.retry.max_delay <= 0:
            raise ValueError(f"max_delay must be positive, got {self.retry.max_delay}")
```

### 4. Message Queue (`queue.py`)

```python
import asyncio
from typing import Optional

from notification_system.exceptions import QueueFullError
from notification_system.models import Channel, NotificationTask


class MessageQueue:
    """Per-channel message queue system using asyncio.Queue.

    Maintains separate asyncio.Queue instances for each notification
    channel. Supports configurable max depth per channel and provides
    depth/pending count queries.
    """

    def __init__(self, max_depth: int = 10000) -> None:
        self._max_depth = max_depth
        self._queues: dict[Channel, asyncio.Queue[NotificationTask]] = {
            channel: asyncio.Queue(maxsize=max_depth)
            for channel in Channel
        }

    async def enqueue(self, task: NotificationTask) -> None:
        """Enqueue a notification task to the appropriate channel queue.

        Args:
            task: The notification task to enqueue.

        Raises:
            QueueFullError: If the channel queue is at max capacity.
        """
        queue = self._queues[task.channel]
        if queue.full():
            raise QueueFullError(task.channel.value, self._max_depth)
        await queue.put(task)

    async def dequeue(self, channel: Channel) -> NotificationTask:
        """Dequeue the next task from a channel queue (blocks if empty).

        Args:
            channel: The channel to dequeue from.

        Returns:
            The next NotificationTask in FIFO order.
        """
        return await self._queues[channel].get()

    def dequeue_nowait(self, channel: Channel) -> Optional[NotificationTask]:
        """Non-blocking dequeue. Returns None if queue is empty."""
        try:
            return self._queues[channel].get_nowait()
        except asyncio.QueueEmpty:
            return None

    def depth(self, channel: Channel) -> int:
        """Current number of pending tasks in a channel queue."""
        return self._queues[channel].qsize()

    def is_full(self, channel: Channel) -> bool:
        """Check if a channel queue is at max capacity."""
        return self._queues[channel].full()

    def is_empty(self, channel: Channel) -> bool:
        """Check if a channel queue has no pending tasks."""
        return self._queues[channel].empty()
```

### 5. Provider Interface (`provider.py`)

```python
import asyncio
import random
import uuid
from abc import ABC, abstractmethod

from notification_system.models import Channel, DeliveryResult, NotificationTask


class Provider(ABC):
    """Abstract interface for notification delivery providers.

    Each provider handles delivery for a specific channel type.
    Implementations must be async-compatible.
    """

    @abstractmethod
    async def deliver(self, task: NotificationTask) -> DeliveryResult:
        """Deliver a notification through this provider.

        Args:
            task: The notification task containing recipient and content.

        Returns:
            DeliveryResult with success status and provider message ID.
        """
        ...

    @property
    @abstractmethod
    def channel(self) -> Channel:
        """The channel this provider handles."""
        ...


class SimulatedProvider(Provider):
    """Simulated provider for testing with configurable behavior.

    Simulates delivery with configurable latency and failure rate.
    Useful for testing the full pipeline without real third-party services.
    """

    def __init__(
        self,
        channel: Channel,
        latency_ms: float = 50.0,
        failure_rate: float = 0.0,
    ) -> None:
        self._channel = channel
        self._latency_ms = latency_ms
        self._failure_rate = failure_rate

    @property
    def channel(self) -> Channel:
        return self._channel

    async def deliver(self, task: NotificationTask) -> DeliveryResult:
        """Simulate delivery with configurable latency and failure."""
        # Simulate network latency
        await asyncio.sleep(self._latency_ms / 1000.0)

        # Simulate random failures
        if random.random() < self._failure_rate:
            return DeliveryResult(
                success=False,
                error="Simulated delivery failure",
                retryable=True,
            )

        return DeliveryResult(
            success=True,
            provider_message_id=f"sim_{uuid.uuid4().hex[:12]}",
        )
```

### 6. Worker Pool (`worker.py`)

```python
import asyncio
import logging
from typing import Optional

from notification_system.exceptions import DeliveryError
from notification_system.models import Channel, NotificationStatus, NotificationTask
from notification_system.provider import Provider
from notification_system.queue import MessageQueue
from notification_system.retry import RetryHandler
from notification_system.tracker import EventTracker

logger = logging.getLogger(__name__)


class Worker:
    """A single async worker that consumes tasks from a channel queue.

    Pulls tasks, invokes the provider, records events, and handles
    failures by delegating to the RetryHandler.
    """

    def __init__(
        self,
        worker_id: str,
        channel: Channel,
        queue: MessageQueue,
        provider: Provider,
        tracker: EventTracker,
        retry_handler: RetryHandler,
    ) -> None:
        self._worker_id = worker_id
        self._channel = channel
        self._queue = queue
        self._provider = provider
        self._tracker = tracker
        self._retry_handler = retry_handler
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the worker loop."""
        self._running = True
        self._task = asyncio.current_task()
        while self._running:
            try:
                notification_task = await asyncio.wait_for(
                    self._queue.dequeue(self._channel), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            await self._process_task(notification_task)

    async def stop(self) -> None:
        """Signal the worker to stop after current task completes."""
        self._running = False

    async def _process_task(self, task: NotificationTask) -> None:
        """Process a single notification task through the provider."""
        self._tracker.record(task.notification_id, task.channel, NotificationStatus.SENDING)

        result = await self._provider.deliver(task)

        if result.success:
            self._tracker.record(task.notification_id, task.channel, NotificationStatus.SENT)
        else:
            error = DeliveryError(
                notification_id=task.notification_id,
                channel=task.channel.value,
                reason=result.error or "Unknown error",
                retryable=result.retryable,
            )
            await self._retry_handler.handle_failure(task, error)


class WorkerPool:
    """Pool of async workers per notification channel.

    Manages lifecycle of worker tasks: start, stop, graceful shutdown.
    Spawns a configurable number of workers per channel.
    """

    def __init__(
        self,
        queue: MessageQueue,
        providers: dict[Channel, Provider],
        tracker: EventTracker,
        retry_handler: RetryHandler,
        workers_per_channel: int = 3,
    ) -> None:
        self._queue = queue
        self._providers = providers
        self._tracker = tracker
        self._retry_handler = retry_handler
        self._workers_per_channel = workers_per_channel
        self._workers: dict[Channel, list[Worker]] = {}
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        """Start all worker tasks for all channels."""
        for channel in Channel:
            provider = self._providers.get(channel)
            if provider is None:
                continue
            self._workers[channel] = []
            for i in range(self._workers_per_channel):
                worker = Worker(
                    worker_id=f"{channel.value}_worker_{i}",
                    channel=channel,
                    queue=self._queue,
                    provider=provider,
                    tracker=self._tracker,
                    retry_handler=self._retry_handler,
                )
                self._workers[channel].append(worker)
                task = asyncio.create_task(worker.start())
                self._tasks.append(task)

    async def stop(self, graceful: bool = True) -> None:
        """Stop all workers.

        Args:
            graceful: If True, wait for in-flight deliveries to complete.
        """
        for channel_workers in self._workers.values():
            for worker in channel_workers:
                await worker.stop()

        if graceful:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        else:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

        self._tasks.clear()
        self._workers.clear()
```

### 7. Notification Templates (`templates.py`)

```python
import re
from typing import Optional

from notification_system.exceptions import TemplateRenderError


class NotificationTemplate:
    """Notification template with named placeholder substitution.

    Supports {variable_name} syntax for placeholders in both title and body.
    Templates are registered by unique ID for reuse.
    """

    PLACEHOLDER_PATTERN = re.compile(r"\{(\w+)\}")

    def __init__(self, template_id: str, title: str, body: str) -> None:
        self._template_id = template_id
        self._title = title
        self._body = body

    @property
    def template_id(self) -> str:
        return self._template_id

    @property
    def title_template(self) -> str:
        return self._title

    @property
    def body_template(self) -> str:
        return self._body

    def get_required_keys(self) -> set[str]:
        """Extract all placeholder names from title and body."""
        title_keys = set(self.PLACEHOLDER_PATTERN.findall(self._title))
        body_keys = set(self.PLACEHOLDER_PATTERN.findall(self._body))
        return title_keys | body_keys

    def render(self, params: dict[str, str]) -> tuple[str, str]:
        """Render the template with the given parameters.

        Args:
            params: Dictionary mapping placeholder names to values.

        Returns:
            Tuple of (rendered_title, rendered_body).

        Raises:
            TemplateRenderError: If required placeholders are missing.
        """
        required = self.get_required_keys()
        missing = required - set(params.keys())
        if missing:
            raise TemplateRenderError(self._template_id, sorted(missing))

        rendered_title = self.PLACEHOLDER_PATTERN.sub(
            lambda m: params[m.group(1)], self._title
        )
        rendered_body = self.PLACEHOLDER_PATTERN.sub(
            lambda m: params[m.group(1)], self._body
        )
        return rendered_title, rendered_body


class TemplateRegistry:
    """Registry for notification templates, keyed by template ID."""

    def __init__(self) -> None:
        self._templates: dict[str, NotificationTemplate] = {}

    def register(self, template: NotificationTemplate) -> None:
        """Register a template by its ID."""
        self._templates[template.template_id] = template

    def get(self, template_id: str) -> Optional[NotificationTemplate]:
        """Retrieve a template by ID. Returns None if not found."""
        return self._templates.get(template_id)

    def list_ids(self) -> list[str]:
        """List all registered template IDs."""
        return list(self._templates.keys())
```

### 8. Rate Limiter (`rate_limiter.py`)

```python
import time
from collections import defaultdict

from notification_system.config import RateLimitConfig
from notification_system.exceptions import RateLimitExceededError
from notification_system.models import Channel


class SlidingWindowRateLimiter:
    """Sliding window rate limiter for per-user, per-channel frequency caps.

    Uses a sorted list of timestamps per (user, channel) pair. On each
    check, removes expired timestamps outside the window, then checks
    if the count exceeds the limit.

    Supports both per-channel and global (cross-channel) rate limits.
    """

    def __init__(
        self,
        per_channel_limits: dict[Channel, RateLimitConfig],
        global_limit: RateLimitConfig,
        clock: callable = None,
    ) -> None:
        self._per_channel_limits = per_channel_limits
        self._global_limit = global_limit
        self._clock = clock or time.monotonic
        # (user_id, channel) -> list of timestamps
        self._channel_windows: dict[tuple[str, Channel], list[float]] = defaultdict(list)
        # user_id -> list of timestamps (global across channels)
        self._global_windows: dict[str, list[float]] = defaultdict(list)

    def check(self, user_id: str, channel: Channel) -> None:
        """Check if a notification is allowed under rate limits.

        Args:
            user_id: The recipient user ID.
            channel: The notification channel.

        Raises:
            RateLimitExceededError: If the rate limit is exceeded.
        """
        now = self._clock()

        # Check per-channel limit
        channel_config = self._per_channel_limits.get(channel)
        if channel_config:
            key = (user_id, channel)
            self._prune(self._channel_windows[key], now - channel_config.window_seconds)
            if len(self._channel_windows[key]) >= channel_config.max_count:
                raise RateLimitExceededError(
                    user_id, channel.value, channel_config.max_count, channel_config.window_seconds
                )

        # Check global limit
        self._prune(self._global_windows[user_id], now - self._global_limit.window_seconds)
        if len(self._global_windows[user_id]) >= self._global_limit.max_count:
            raise RateLimitExceededError(
                user_id, "global", self._global_limit.max_count, self._global_limit.window_seconds
            )

    def record(self, user_id: str, channel: Channel) -> None:
        """Record a notification send for rate tracking.

        Args:
            user_id: The recipient user ID.
            channel: The notification channel.
        """
        now = self._clock()
        self._channel_windows[(user_id, channel)].append(now)
        self._global_windows[user_id].append(now)

    def _prune(self, timestamps: list[float], cutoff: float) -> None:
        """Remove timestamps older than the cutoff."""
        while timestamps and timestamps[0] < cutoff:
            timestamps.pop(0)
```

### 9. Deduplicator (`dedup.py`)

```python
import hashlib
import time
from typing import Optional

from notification_system.exceptions import DuplicateNotificationError
from notification_system.models import Channel


class Deduplicator:
    """Content-hash based deduplication with configurable TTL.

    Computes a deduplication key from (recipient_id, channel, content_hash).
    Maintains an in-memory dict of dedup keys with expiration timestamps.
    Automatically expires old entries on access.
    """

    def __init__(self, window_seconds: float = 300.0, clock: callable = None) -> None:
        self._window_seconds = window_seconds
        self._clock = clock or time.monotonic
        # dedup_key -> (notification_id, expiry_timestamp)
        self._entries: dict[str, tuple[str, float]] = {}

    def compute_key(self, recipient_id: str, channel: Channel, content: str) -> str:
        """Compute a deduplication key from recipient, channel, and content.

        Args:
            recipient_id: The recipient user ID.
            channel: The notification channel.
            content: The notification content (title + body).

        Returns:
            SHA-256 hex digest of the combined key components.
        """
        raw = f"{recipient_id}:{channel.value}:{content}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def check(self, dedup_key: str) -> Optional[str]:
        """Check if a dedup key exists and is not expired.

        Args:
            dedup_key: The deduplication key to check.

        Returns:
            The original notification_id if duplicate, None otherwise.
        """
        self._expire_old_entries()
        entry = self._entries.get(dedup_key)
        if entry is not None:
            notification_id, expiry = entry
            if self._clock() < expiry:
                return notification_id
            else:
                del self._entries[dedup_key]
        return None

    def record(self, dedup_key: str, notification_id: str) -> None:
        """Record a dedup key with the associated notification ID.

        Args:
            dedup_key: The deduplication key.
            notification_id: The notification ID to associate.
        """
        expiry = self._clock() + self._window_seconds
        self._entries[dedup_key] = (notification_id, expiry)

    def check_and_record(
        self, recipient_id: str, channel: Channel, content: str, notification_id: str
    ) -> None:
        """Check for duplicate and record if new.

        Args:
            recipient_id: The recipient user ID.
            channel: The notification channel.
            content: The notification content.
            notification_id: The new notification's ID.

        Raises:
            DuplicateNotificationError: If a duplicate is detected.
        """
        key = self.compute_key(recipient_id, channel, content)
        original_id = self.check(key)
        if original_id is not None:
            raise DuplicateNotificationError(original_id)
        self.record(key, notification_id)

    def _expire_old_entries(self) -> None:
        """Remove all expired entries."""
        now = self._clock()
        expired_keys = [k for k, (_, expiry) in self._entries.items() if now >= expiry]
        for k in expired_keys:
            del self._entries[k]

    @property
    def entry_count(self) -> int:
        """Number of active (non-expired) dedup entries."""
        self._expire_old_entries()
        return len(self._entries)
```

### 10. Retry Handler (`retry.py`)

```python
import asyncio
import logging
import random
from datetime import datetime, timezone

from notification_system.config import RetryConfig
from notification_system.exceptions import DeliveryError
from notification_system.models import NotificationStatus, NotificationTask
from notification_system.queue import MessageQueue
from notification_system.tracker import EventTracker
from notification_system.log import NotificationLog

logger = logging.getLogger(__name__)


class RetryHandler:
    """Handles failed notification deliveries with exponential backoff and jitter.

    Computes delay as: min(base_delay * 2^attempt + jitter, max_delay)
    where jitter is a random value in [0, jitter_factor * base_delay * 2^attempt].

    Non-retryable errors (invalid token, permanent rejection) skip retry
    and immediately mark the notification as FAILED.
    """

    def __init__(
        self,
        config: RetryConfig,
        queue: MessageQueue,
        tracker: EventTracker,
        notification_log: NotificationLog,
    ) -> None:
        self._config = config
        self._queue = queue
        self._tracker = tracker
        self._log = notification_log

    def compute_delay(self, attempt: int) -> float:
        """Compute the backoff delay for a given attempt number.

        Formula: min(base_delay * 2^attempt + jitter, max_delay)

        Args:
            attempt: The retry attempt number (0-indexed).

        Returns:
            Delay in seconds.
        """
        exponential = self._config.base_delay * (2 ** attempt)
        jitter = random.uniform(0, self._config.jitter_factor * exponential)
        return min(exponential + jitter, self._config.max_delay)

    async def handle_failure(self, task: NotificationTask, error: DeliveryError) -> None:
        """Handle a failed delivery attempt.

        If the error is retryable and max retries not exceeded, re-enqueue
        with exponential backoff delay. Otherwise, mark as permanently failed.

        Args:
            task: The failed notification task.
            error: The delivery error with retryable flag.
        """
        # Log the retry attempt
        self._log.record_attempt(
            notification_id=task.notification_id,
            recipient_id=task.request.recipient_id,
            channel=task.channel,
            content_summary=task.request.body[:100],
            status=NotificationStatus.FAILED,
            attempt=task.attempt + 1,
            error_details=error.reason,
        )

        # Non-retryable errors: immediate permanent failure
        if not error.retryable:
            self._tracker.record(
                task.notification_id, task.channel, NotificationStatus.FAILED
            )
            return

        # Check max retries
        if task.attempt >= self._config.max_retries:
            self._tracker.record(
                task.notification_id, task.channel, NotificationStatus.FAILED
            )
            return

        # Compute delay and schedule retry
        delay = self.compute_delay(task.attempt)
        await asyncio.sleep(delay)

        # Re-enqueue with incremented attempt counter
        retry_task = NotificationTask(
            notification_id=task.notification_id,
            request=task.request,
            channel=task.channel,
            attempt=task.attempt + 1,
            created_at=task.created_at,
            scheduled_at=datetime.now(timezone.utc),
        )
        await self._queue.enqueue(retry_task)
```

### 11. Notification Settings (`settings.py`)

```python
from collections import defaultdict

from notification_system.models import Channel


class NotificationSettings:
    """Per-user, per-channel notification preference store.

    Defaults to opted-in for all channels when a user is first seen.
    Supports opt-in, opt-out, bulk updates, and preference queries.
    """

    def __init__(self) -> None:
        # user_id -> {channel -> opted_in}
        self._preferences: dict[str, dict[Channel, bool]] = {}

    def _ensure_user(self, user_id: str) -> None:
        """Initialize default preferences (all opted-in) for a new user."""
        if user_id not in self._preferences:
            self._preferences[user_id] = {channel: True for channel in Channel}

    def is_opted_in(self, user_id: str, channel: Channel) -> bool:
        """Check if a user is opted in for a channel.

        Args:
            user_id: The user identifier.
            channel: The notification channel.

        Returns:
            True if opted in (default), False if opted out.
        """
        self._ensure_user(user_id)
        return self._preferences[user_id][channel]

    def opt_out(self, user_id: str, channel: Channel) -> None:
        """Opt a user out of a notification channel.

        Args:
            user_id: The user identifier.
            channel: The channel to opt out of.
        """
        self._ensure_user(user_id)
        self._preferences[user_id][channel] = False

    def opt_in(self, user_id: str, channel: Channel) -> None:
        """Opt a user back in to a notification channel.

        Args:
            user_id: The user identifier.
            channel: The channel to opt in to.
        """
        self._ensure_user(user_id)
        self._preferences[user_id][channel] = True

    def get_all_preferences(self, user_id: str) -> dict[Channel, bool]:
        """Get all channel preferences for a user.

        Args:
            user_id: The user identifier.

        Returns:
            Dict mapping each Channel to its opt-in status.
        """
        self._ensure_user(user_id)
        return dict(self._preferences[user_id])

    def bulk_update(self, user_id: str, preferences: dict[Channel, bool]) -> None:
        """Update multiple channel preferences in a single operation.

        Args:
            user_id: The user identifier.
            preferences: Dict mapping channels to their new opt-in status.
        """
        self._ensure_user(user_id)
        for channel, opted_in in preferences.items():
            self._preferences[user_id][channel] = opted_in
```

### 12. Contact Info Store (`contacts.py`)

```python
from typing import Optional

from notification_system.models import Channel, ContactInfo


class ContactInfoStore:
    """In-memory store for user contact information.

    Manages email addresses, phone numbers, and device tokens.
    Supports multiple device tokens per user for push channels.
    """

    def __init__(self) -> None:
        self._contacts: dict[str, ContactInfo] = {}

    def get(self, user_id: str) -> Optional[ContactInfo]:
        """Retrieve contact info for a user."""
        return self._contacts.get(user_id)

    def set(self, contact: ContactInfo) -> None:
        """Store or update contact info for a user."""
        self._contacts[contact.user_id] = contact

    def remove(self, user_id: str) -> None:
        """Remove all contact info for a user."""
        self._contacts.pop(user_id, None)

    def add_device_token(self, user_id: str, token: str) -> None:
        """Add a device token for a user.

        Args:
            user_id: The user identifier.
            token: The device token to add.
        """
        contact = self._contacts.get(user_id)
        if contact is None:
            contact = ContactInfo(user_id=user_id)
            self._contacts[user_id] = contact
        if token not in contact.device_tokens:
            contact.device_tokens.append(token)

    def remove_device_token(self, user_id: str, token: str) -> None:
        """Remove a device token for a user.

        Args:
            user_id: The user identifier.
            token: The device token to remove.
        """
        contact = self._contacts.get(user_id)
        if contact and token in contact.device_tokens:
            contact.device_tokens.remove(token)

    def resolve_endpoint(self, user_id: str, channel: Channel) -> Optional[str]:
        """Resolve the delivery endpoint for a user and channel.

        Args:
            user_id: The user identifier.
            channel: The notification channel.

        Returns:
            The endpoint string (email, phone, or first device token),
            or None if no contact info exists for that channel.
        """
        contact = self._contacts.get(user_id)
        if contact is None:
            return None

        if channel == Channel.EMAIL:
            return contact.email
        elif channel == Channel.SMS:
            return contact.phone
        elif channel in (Channel.IOS_PUSH, Channel.ANDROID_PUSH):
            return contact.device_tokens[0] if contact.device_tokens else None
        return None
```

### 13. Event Tracker (`tracker.py`)

```python
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from notification_system.models import Channel, NotificationEvent, NotificationStatus, VALID_TRANSITIONS


class EventTracker:
    """Lifecycle event tracker with state machine validation and analytics.

    Records state transitions for each notification. Validates that
    transitions follow the defined state machine. Provides aggregate
    analytics and per-channel statistics.
    """

    def __init__(self, clock: callable = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # notification_id -> list of events (ordered by time)
        self._events: dict[str, list[NotificationEvent]] = defaultdict(list)

    def record(
        self,
        notification_id: str,
        channel: Channel,
        status: NotificationStatus,
        details: Optional[str] = None,
    ) -> None:
        """Record a lifecycle event for a notification.

        Args:
            notification_id: The notification identifier.
            channel: The notification channel.
            status: The new status.
            details: Optional details about the transition.
        """
        event = NotificationEvent(
            notification_id=notification_id,
            channel=channel,
            status=status,
            timestamp=self._clock(),
            details=details,
        )
        self._events[notification_id].append(event)

    def get_history(self, notification_id: str) -> list[NotificationEvent]:
        """Get the full event history for a notification.

        Args:
            notification_id: The notification identifier.

        Returns:
            List of NotificationEvent in chronological order.
        """
        return list(self._events.get(notification_id, []))

    def get_current_status(self, notification_id: str) -> Optional[NotificationStatus]:
        """Get the current (latest) status for a notification."""
        events = self._events.get(notification_id)
        if not events:
            return None
        return events[-1].status

    def get_aggregate_counts(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> dict[str, int]:
        """Get aggregate counts of notifications by final status.

        Args:
            start_time: Optional start of time range filter.
            end_time: Optional end of time range filter.

        Returns:
            Dict with keys: total_sent, total_delivered, total_failed, total_clicked.
        """
        counts = {"total_sent": 0, "total_delivered": 0, "total_failed": 0, "total_clicked": 0}
        for notification_id, events in self._events.items():
            if not events:
                continue
            last_event = events[-1]
            if start_time and last_event.timestamp < start_time:
                continue
            if end_time and last_event.timestamp > end_time:
                continue
            if last_event.status == NotificationStatus.SENT:
                counts["total_sent"] += 1
            elif last_event.status == NotificationStatus.DELIVERED:
                counts["total_delivered"] += 1
            elif last_event.status == NotificationStatus.FAILED:
                counts["total_failed"] += 1
            elif last_event.status == NotificationStatus.CLICKED:
                counts["total_clicked"] += 1
        return counts

    def get_channel_stats(self, channel: Channel) -> dict[str, float]:
        """Get delivery statistics for a specific channel.

        Returns:
            Dict with success_rate, failure_rate, and total count.
        """
        total = 0
        success = 0
        failed = 0
        for events in self._events.values():
            if not events:
                continue
            # Check if this notification is for the given channel
            if events[0].channel != channel:
                continue
            total += 1
            last_status = events[-1].status
            if last_status in (NotificationStatus.SENT, NotificationStatus.DELIVERED, NotificationStatus.CLICKED):
                success += 1
            elif last_status == NotificationStatus.FAILED:
                failed += 1

        return {
            "total": total,
            "success_rate": success / total if total > 0 else 0.0,
            "failure_rate": failed / total if total > 0 else 0.0,
        }
```

### 14. Notification Log (`log.py`)

```python
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

from notification_system.models import Channel, LogEntry, NotificationStatus


class NotificationLogBackend(ABC):
    """Abstract interface for notification log persistence."""

    @abstractmethod
    def append(self, entry: LogEntry) -> None:
        """Append a log entry."""
        ...

    @abstractmethod
    def query(
        self,
        notification_id: Optional[str] = None,
        recipient_id: Optional[str] = None,
        channel: Optional[Channel] = None,
        status: Optional[NotificationStatus] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> list[LogEntry]:
        """Query log entries by various criteria."""
        ...

    @abstractmethod
    def cleanup(self, retention_seconds: float) -> int:
        """Remove entries older than retention period. Returns count removed."""
        ...


class InMemoryNotificationLog(NotificationLogBackend):
    """In-memory notification log with query and retention support."""

    def __init__(self) -> None:
        self._entries: list[LogEntry] = []

    def append(self, entry: LogEntry) -> None:
        """Append a log entry to the in-memory store."""
        self._entries.append(entry)

    def query(
        self,
        notification_id: Optional[str] = None,
        recipient_id: Optional[str] = None,
        channel: Optional[Channel] = None,
        status: Optional[NotificationStatus] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> list[LogEntry]:
        """Query entries matching all specified criteria."""
        results = []
        for entry in self._entries:
            if notification_id and entry.notification_id != notification_id:
                continue
            if recipient_id and entry.recipient_id != recipient_id:
                continue
            if channel and entry.channel != channel:
                continue
            if status and entry.status != status:
                continue
            if start_time and entry.timestamp < start_time:
                continue
            if end_time and entry.timestamp > end_time:
                continue
            results.append(entry)
        return results

    def cleanup(self, retention_seconds: float) -> int:
        """Remove entries older than retention period."""
        cutoff = datetime.now(timezone.utc).timestamp() - retention_seconds
        original_count = len(self._entries)
        self._entries = [
            e for e in self._entries if e.timestamp.timestamp() >= cutoff
        ]
        return original_count - len(self._entries)


class NotificationLog:
    """High-level notification log facade.

    Wraps a NotificationLogBackend and provides convenience methods
    for recording attempts and querying history.
    """

    def __init__(self, backend: Optional[NotificationLogBackend] = None) -> None:
        self._backend = backend or InMemoryNotificationLog()

    def record_attempt(
        self,
        notification_id: str,
        recipient_id: str,
        channel: Channel,
        content_summary: str,
        status: NotificationStatus,
        attempt: int = 1,
        error_details: Optional[str] = None,
    ) -> None:
        """Record a notification attempt."""
        entry = LogEntry(
            notification_id=notification_id,
            recipient_id=recipient_id,
            channel=channel,
            content_summary=content_summary,
            timestamp=datetime.now(timezone.utc),
            status=status,
            attempt=attempt,
            error_details=error_details,
        )
        self._backend.append(entry)

    def query(self, **kwargs) -> list[LogEntry]:
        """Query log entries. Delegates to backend."""
        return self._backend.query(**kwargs)

    def cleanup(self, retention_seconds: float) -> int:
        """Clean up old entries. Returns count removed."""
        return self._backend.cleanup(retention_seconds)
```

### 15. Notification Service Orchestrator (`service.py`)

```python
import uuid
from typing import Optional

from notification_system.config import NotificationConfig
from notification_system.contacts import ContactInfoStore
from notification_system.dedup import Deduplicator
from notification_system.exceptions import ValidationError
from notification_system.models import (
    Channel,
    NotificationRequest,
    NotificationStatus,
    NotificationTask,
)
from notification_system.queue import MessageQueue
from notification_system.rate_limiter import SlidingWindowRateLimiter
from notification_system.settings import NotificationSettings
from notification_system.templates import NotificationTemplate, TemplateRegistry
from notification_system.tracker import EventTracker


class NotificationService:
    """Central orchestrator for the notification pipeline.

    Pipeline: validate → check preferences → rate limit → dedup → enqueue.

    Coordinates all subsystems and provides the unified public API
    for sending notifications, managing templates, and querying status.
    """

    def __init__(
        self,
        config: Optional[NotificationConfig] = None,
        queue: Optional[MessageQueue] = None,
        settings: Optional[NotificationSettings] = None,
        contacts: Optional[ContactInfoStore] = None,
        rate_limiter: Optional[SlidingWindowRateLimiter] = None,
        deduplicator: Optional[Deduplicator] = None,
        tracker: Optional[EventTracker] = None,
        template_registry: Optional[TemplateRegistry] = None,
    ) -> None:
        self._config = config or NotificationConfig()
        self._queue = queue or MessageQueue(max_depth=self._config.queue_max_depth)
        self._settings = settings or NotificationSettings()
        self._contacts = contacts or ContactInfoStore()
        self._rate_limiter = rate_limiter or SlidingWindowRateLimiter(
            per_channel_limits=self._config.per_channel_rate_limits,
            global_limit=self._config.global_rate_limit,
        )
        self._deduplicator = deduplicator or Deduplicator(
            window_seconds=self._config.dedup_window_seconds
        )
        self._tracker = tracker or EventTracker()
        self._templates = template_registry or TemplateRegistry()

    async def send(self, request: NotificationRequest) -> str:
        """Send a single notification through the pipeline.

        Pipeline steps:
        1. Validate request (contact info exists)
        2. Check user preferences (opt-in/out)
        3. Resolve content (template or direct)
        4. Rate limit check
        5. Deduplication check
        6. Enqueue to channel queue

        Args:
            request: The notification request.

        Returns:
            Unique notification identifier.

        Raises:
            ValidationError: If request is invalid.
            RateLimitExceededError: If rate limit exceeded.
            DuplicateNotificationError: If duplicate detected.
        """
        notification_id = uuid.uuid4().hex

        # 1. Validate: check contact info exists
        endpoint = self._contacts.resolve_endpoint(request.recipient_id, request.channel)
        if endpoint is None:
            raise ValidationError(
                f"No contact info for user '{request.recipient_id}' on channel '{request.channel.value}'",
                field="recipient_id",
            )

        # 2. Check preferences
        if not self._settings.is_opted_in(request.recipient_id, request.channel):
            self._tracker.record(
                notification_id, request.channel, NotificationStatus.UNSUBSCRIBED
            )
            raise ValidationError(
                f"User '{request.recipient_id}' has opted out of '{request.channel.value}'"
            )

        # Record CREATED event
        self._tracker.record(notification_id, request.channel, NotificationStatus.CREATED)

        # 3. Resolve content
        title, body = self._resolve_content(request)

        # 4. Rate limit
        self._rate_limiter.check(request.recipient_id, request.channel)
        self._rate_limiter.record(request.recipient_id, request.channel)

        # 5. Deduplication
        content = f"{title}:{body}"
        self._deduplicator.check_and_record(
            request.recipient_id, request.channel, content, notification_id
        )

        # 6. Enqueue
        task = NotificationTask(
            notification_id=notification_id,
            request=request,
            channel=request.channel,
        )
        await self._queue.enqueue(task)
        self._tracker.record(notification_id, request.channel, NotificationStatus.QUEUED)

        return notification_id

    async def send_batch(self, requests: list[NotificationRequest]) -> list[str]:
        """Send notifications to multiple recipients.

        Args:
            requests: List of notification requests.

        Returns:
            List of notification identifiers (one per request).
            Failed requests return empty string in their position.
        """
        results = []
        for request in requests:
            try:
                nid = await self.send(request)
                results.append(nid)
            except Exception:
                results.append("")
        return results

    def _resolve_content(self, request: NotificationRequest) -> tuple[str, str]:
        """Resolve notification content from template or direct fields."""
        if request.template_id:
            template = self._templates.get(request.template_id)
            if template is None:
                raise ValidationError(
                    f"Template '{request.template_id}' not found",
                    field="template_id",
                )
            return template.render(request.template_params)
        return request.title, request.body
```

### 16. Public API (`__init__.py`)

```python
"""Notification System - An asyncio-based in-process notification pipeline."""

from notification_system.config import NotificationConfig, RateLimitConfig, RetryConfig
from notification_system.contacts import ContactInfoStore
from notification_system.dedup import Deduplicator
from notification_system.exceptions import (
    DeliveryError,
    DuplicateNotificationError,
    NotificationError,
    QueueFullError,
    RateLimitExceededError,
    TemplateRenderError,
    ValidationError,
)
from notification_system.log import NotificationLog, NotificationLogBackend, InMemoryNotificationLog
from notification_system.models import (
    Channel,
    ContactInfo,
    DeliveryResult,
    LogEntry,
    NotificationEvent,
    NotificationRequest,
    NotificationStatus,
    NotificationTask,
    VALID_TRANSITIONS,
)
from notification_system.provider import Provider, SimulatedProvider
from notification_system.queue import MessageQueue
from notification_system.rate_limiter import SlidingWindowRateLimiter
from notification_system.retry import RetryHandler
from notification_system.service import NotificationService
from notification_system.settings import NotificationSettings
from notification_system.templates import NotificationTemplate, TemplateRegistry
from notification_system.tracker import EventTracker
from notification_system.worker import Worker, WorkerPool

__all__ = [
    # Core
    "NotificationService",
    "NotificationConfig",
    "RateLimitConfig",
    "RetryConfig",
    # Models
    "Channel",
    "ContactInfo",
    "DeliveryResult",
    "LogEntry",
    "NotificationEvent",
    "NotificationRequest",
    "NotificationStatus",
    "NotificationTask",
    "VALID_TRANSITIONS",
    # Queue & Workers
    "MessageQueue",
    "Worker",
    "WorkerPool",
    # Provider
    "Provider",
    "SimulatedProvider",
    # Templates
    "NotificationTemplate",
    "TemplateRegistry",
    # Rate Limiting & Dedup
    "SlidingWindowRateLimiter",
    "Deduplicator",
    # Retry
    "RetryHandler",
    # Settings & Contacts
    "NotificationSettings",
    "ContactInfoStore",
    # Tracking & Logging
    "EventTracker",
    "NotificationLog",
    "NotificationLogBackend",
    "InMemoryNotificationLog",
    # Exceptions
    "NotificationError",
    "ValidationError",
    "QueueFullError",
    "RateLimitExceededError",
    "DuplicateNotificationError",
    "TemplateRenderError",
    "DeliveryError",
]
```

### 17. HTTP Demo Server (`examples/demo_server.py`)

```python
"""Lightweight HTTP demo server using only Python stdlib.

Endpoints:
    POST /notifications         - Send a notification
    GET  /notifications/<id>    - Get notification status and event history
    GET  /analytics             - Get aggregate delivery analytics
    GET  /analytics/<channel>   - Get per-channel statistics
    GET  /settings/<user_id>    - Get user notification preferences
    PUT  /settings/<user_id>    - Update user notification preferences
    POST /templates             - Create a notification template
    GET  /templates             - List all templates
    POST /templates/<id>/render - Preview a rendered template

Usage:
    python -m examples.demo_server --port 8000
"""

import asyncio
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

from notification_system import (
    Channel,
    ContactInfo,
    ContactInfoStore,
    NotificationConfig,
    NotificationRequest,
    NotificationService,
    NotificationSettings,
    NotificationTemplate,
    TemplateRegistry,
    EventTracker,
    RateLimitExceededError,
    DuplicateNotificationError,
    ValidationError,
)


class NotificationHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the notification system demo."""

    service: NotificationService
    tracker: EventTracker
    settings: NotificationSettings
    templates: TemplateRegistry
    loop: asyncio.AbstractEventLoop

    def do_POST(self) -> None:
        """Handle POST requests."""
        if self.path == "/notifications":
            self._handle_send_notification()
        elif self.path.startswith("/templates") and self.path.endswith("/render"):
            self._handle_render_template()
        elif self.path == "/templates":
            self._handle_create_template()
        else:
            self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_GET(self) -> None:
        """Handle GET requests."""
        if self.path.startswith("/notifications/"):
            notification_id = self.path[len("/notifications/"):]
            self._handle_get_status(notification_id)
        elif self.path.startswith("/analytics/"):
            channel_str = self.path[len("/analytics/"):]
            self._handle_channel_analytics(channel_str)
        elif self.path == "/analytics":
            self._handle_aggregate_analytics()
        elif self.path.startswith("/settings/"):
            user_id = self.path[len("/settings/"):]
            self._handle_get_settings(user_id)
        elif self.path == "/templates":
            self._handle_list_templates()
        else:
            self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_PUT(self) -> None:
        """Handle PUT requests."""
        if self.path.startswith("/settings/"):
            user_id = self.path[len("/settings/"):]
            self._handle_update_settings(user_id)
        else:
            self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _handle_send_notification(self) -> None:
        """POST /notifications - Send a notification."""
        data = self._read_json()
        if data is None:
            return

        try:
            request = NotificationRequest(
                recipient_id=data["recipient_id"],
                channel=Channel(data["channel"]),
                title=data.get("title", ""),
                body=data.get("body", ""),
                template_id=data.get("template_id"),
                template_params=data.get("template_params", {}),
            )
            notification_id = self.loop.run_until_complete(self.service.send(request))
            self._send_json(HTTPStatus.CREATED, {"notification_id": notification_id})
        except RateLimitExceededError as e:
            self._send_error(HTTPStatus.TOO_MANY_REQUESTS, str(e))
        except (ValidationError, DuplicateNotificationError) as e:
            self._send_error(HTTPStatus.BAD_REQUEST, str(e))

    def _handle_get_status(self, notification_id: str) -> None:
        """GET /notifications/<id> - Get status and history."""
        history = self.tracker.get_history(notification_id)
        if not history:
            self._send_error(HTTPStatus.NOT_FOUND, "Notification not found")
            return
        events = [
            {"status": e.status.value, "timestamp": e.timestamp.isoformat(), "channel": e.channel.value}
            for e in history
        ]
        self._send_json(HTTPStatus.OK, {"notification_id": notification_id, "events": events})

    def _handle_aggregate_analytics(self) -> None:
        """GET /analytics - Aggregate delivery counts."""
        counts = self.tracker.get_aggregate_counts()
        self._send_json(HTTPStatus.OK, counts)

    def _handle_channel_analytics(self, channel_str: str) -> None:
        """GET /analytics/<channel> - Per-channel stats."""
        try:
            channel = Channel(channel_str)
        except ValueError:
            self._send_error(HTTPStatus.NOT_FOUND, f"Unknown channel: {channel_str}")
            return
        stats = self.tracker.get_channel_stats(channel)
        self._send_json(HTTPStatus.OK, stats)

    def _handle_get_settings(self, user_id: str) -> None:
        """GET /settings/<user_id> - Get preferences."""
        prefs = self.settings.get_all_preferences(user_id)
        result = {ch.value: opted_in for ch, opted_in in prefs.items()}
        self._send_json(HTTPStatus.OK, {"user_id": user_id, "preferences": result})

    def _handle_update_settings(self, user_id: str) -> None:
        """PUT /settings/<user_id> - Update preferences."""
        data = self._read_json()
        if data is None:
            return
        prefs = {Channel(k): v for k, v in data.get("preferences", {}).items()}
        self.settings.bulk_update(user_id, prefs)
        self._send_json(HTTPStatus.OK, {"status": "updated"})

    def _handle_create_template(self) -> None:
        """POST /templates - Create a template."""
        data = self._read_json()
        if data is None:
            return
        template = NotificationTemplate(
            template_id=data["template_id"],
            title=data.get("title", ""),
            body=data.get("body", ""),
        )
        self.templates.register(template)
        self._send_json(HTTPStatus.CREATED, {"template_id": template.template_id})

    def _handle_list_templates(self) -> None:
        """GET /templates - List all template IDs."""
        self._send_json(HTTPStatus.OK, {"templates": self.templates.list_ids()})

    def _handle_render_template(self) -> None:
        """POST /templates/<id>/render - Preview rendered template."""
        parts = self.path.split("/")
        template_id = parts[2] if len(parts) >= 4 else ""
        data = self._read_json()
        if data is None:
            return
        template = self.templates.get(template_id)
        if template is None:
            self._send_error(HTTPStatus.NOT_FOUND, f"Template '{template_id}' not found")
            return
        try:
            title, body = template.render(data.get("params", {}))
            self._send_json(HTTPStatus.OK, {"title": title, "body": body})
        except Exception as e:
            self._send_error(HTTPStatus.BAD_REQUEST, str(e))

    def _read_json(self) -> Optional[dict]:
        """Read and parse JSON request body."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            self._send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return None

    def _send_json(self, status: HTTPStatus, data: dict) -> None:
        """Send a JSON response."""
        body = json.dumps(data).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        """Send an error JSON response."""
        self._send_json(status, {"error": message})
```

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Concurrency model | asyncio tasks + Queue | Single-threaded async; no threading complexity; native async Queue for backpressure |
| Queue implementation | asyncio.Queue per channel | Built-in maxsize support; FIFO guaranteed; non-blocking put/get |
| Worker pool | asyncio.create_task per worker | Lightweight; cooperative scheduling; easy cancellation |
| Provider abstraction | ABC with async deliver() | Clean interface for testing (simulated) and production (real providers) |
| Rate limiter algorithm | Sliding window (timestamp list) | Accurate frequency tracking; no fixed bucket boundaries; simple implementation |
| Deduplication | SHA-256 content hash + TTL dict | O(1) lookup; deterministic; automatic expiry prevents memory growth |
| Retry strategy | Exponential backoff + jitter | Prevents thundering herd; configurable cap; industry standard |
| Event tracking | In-memory dict of event lists | O(1) append; O(n) query; sufficient for single-process use |
| Notification log | ABC + InMemoryNotificationLog | Pluggable backends; default works without dependencies |
| Template engine | Regex-based {placeholder} substitution | Zero dependencies; simple and predictable; sufficient for notification content |
| Settings store | In-memory dict per user | O(1) lookup; defaults to opted-in; immediate effect on opt-out |
| Contact info | In-memory dict with multi-token support | O(1) lookup; supports multiple device tokens per user |
| ID generation | uuid4 hex | Globally unique; no coordination needed; 128-bit collision resistance |
| Configuration | Nested dataclasses with validation | Type-safe; validated in __post_init__; sensible defaults |
| Project layout | src/ layout with hatchling | Standard Python packaging; clean import paths |
| HTTP demo | stdlib http.server | Zero dependencies; demonstrates full API surface |
| Error handling | Custom exception hierarchy | Typed errors enable precise catch blocks; all inherit NotificationError |
| Clock injection | Optional callable parameter | Enables deterministic testing of time-dependent components |

## Error Handling

| Error Condition | Exception | When Raised |
|----------------|-----------|-------------|
| Missing contact info | `ValidationError` | `send()` with recipient lacking channel contact |
| User opted out | `ValidationError` | `send()` when user has opted out of channel |
| Invalid template ID | `ValidationError` | `send()` with non-existent template_id |
| Queue at capacity | `QueueFullError` | `enqueue()` when channel queue is full |
| Rate limit exceeded | `RateLimitExceededError` | `send()` when user exceeds frequency cap |
| Duplicate notification | `DuplicateNotificationError` | `send()` when content hash matches recent send |
| Template missing keys | `TemplateRenderError` | `render()` with incomplete parameter dict |
| Provider delivery failure | `DeliveryError` | Worker receives failed DeliveryResult from provider |
| Invalid config values | `ValueError` | NotificationConfig.__post_init__ with invalid numerics |

All custom exceptions inherit from `NotificationError` for catch-all handling.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system—essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Channel routing correctness

*For any* valid notification request specifying a channel C, when the notification is successfully enqueued, it SHALL appear in the queue for channel C and SHALL NOT appear in any other channel's queue.

**Validates: Requirements 1.1, 2.1**

### Property 2: Opt-out rejection

*For any* user who has opted out of channel C, submitting a notification request for that user on channel C SHALL be rejected, and the event tracker SHALL record an UNSUBSCRIBED status for that notification.

**Validates: Requirements 1.2, 1.6, 8.3**

### Property 3: Invalid recipient validation

*For any* notification request where the recipient has no contact info registered for the specified channel, the service SHALL raise a ValidationError.

**Validates: Requirements 1.5**

### Property 4: Notification ID uniqueness

*For any* set of N successfully enqueued notifications, all N returned notification identifiers SHALL be distinct.

**Validates: Requirements 1.8**

### Property 5: Queue FIFO ordering

*For any* sequence of notification tasks enqueued to a single channel queue, dequeuing all tasks SHALL return them in the same order they were enqueued (FIFO).

**Validates: Requirements 2.6**

### Property 6: Queue depth invariant

*For any* sequence of enqueue and dequeue operations on a channel queue, the reported depth SHALL equal the number of successful enqueues minus the number of successful dequeues, and SHALL never exceed the configured maximum depth.

**Validates: Requirements 2.3, 2.4, 2.5**

### Property 7: Template rendering substitution

*For any* notification template with placeholders and a complete parameter dictionary, rendering SHALL produce output where every placeholder is replaced by its corresponding value and no `{placeholder}` syntax remains in the result.

**Validates: Requirements 5.1, 5.2**

### Property 8: Template missing placeholder error

*For any* notification template with at least one placeholder, rendering with a parameter dictionary that is missing one or more required keys SHALL raise a TemplateRenderError listing the missing keys.

**Validates: Requirements 5.3**

### Property 9: Template registration round-trip

*For any* notification template registered with a unique ID, retrieving that template by ID SHALL return a template with the same ID, title template, and body template.

**Validates: Requirements 5.6**

### Property 10: Rate limiter enforcement

*For any* user and channel with a configured limit of N notifications per window W, after N notifications are recorded within window W, the next check SHALL raise a RateLimitExceededError.

**Validates: Requirements 6.1, 6.4**

### Property 11: Per-channel rate limit independence

*For any* user, exhausting the rate limit on one channel SHALL NOT affect the rate limit availability on other channels (each channel's limit is tracked independently).

**Validates: Requirements 6.2**

### Property 12: Sliding window expiry

*For any* user and channel, after the configured time window has fully elapsed since all recorded notifications, the rate limiter SHALL accept new notifications (the window slides forward and old entries expire).

**Validates: Requirements 6.6**

### Property 13: Deduplication key determinism

*For any* (recipient_id, channel, content) triple, computing the deduplication key multiple times SHALL always produce the same result. Two triples that differ in any component SHALL produce different keys.

**Validates: Requirements 7.1**

### Property 14: Duplicate suppression within window

*For any* notification that has been successfully recorded in the deduplicator, submitting a second notification with the same (recipient_id, channel, content) within the deduplication window SHALL raise a DuplicateNotificationError containing the original notification's ID.

**Validates: Requirements 7.2, 7.4**

### Property 15: Deduplication TTL expiry

*For any* deduplication entry, after the configured TTL has elapsed, the same (recipient_id, channel, content) triple SHALL be accepted as a new notification (the entry has expired).

**Validates: Requirements 7.3, 7.5**

### Property 16: Notification settings round-trip

*For any* user and channel, opting out and then opting back in SHALL restore the opted-in state. The preference retrieved after any update SHALL match the last value set.

**Validates: Requirements 8.1, 8.3, 8.4**

### Property 17: Default opt-in for new users

*For any* user ID that has not been previously registered, querying their preferences SHALL return opted-in (True) for all channels.

**Validates: Requirements 8.2**

### Property 18: Exponential backoff with cap

*For any* retry attempt number N with configured base_delay B and max_delay M, the computed delay SHALL be at most M and SHALL follow the formula min(B × 2^N + jitter, M) where jitter is non-negative.

**Validates: Requirements 9.1, 9.3**

### Property 19: Retry exhaustion marks permanent failure

*For any* notification that has failed delivery and exhausted the configured maximum retry count, the event tracker SHALL record a FAILED status, and no further retry attempts SHALL be made.

**Validates: Requirements 9.2, 9.4**

### Property 20: Retryable vs non-retryable error classification

*For any* delivery failure marked as non-retryable, the retry handler SHALL immediately mark the notification as FAILED without re-enqueueing, regardless of remaining retry budget.

**Validates: Requirements 9.6**

### Property 21: Event state machine validity

*For any* notification's event history, each consecutive pair of status transitions SHALL be a valid transition according to the defined state machine (VALID_TRANSITIONS map).

**Validates: Requirements 10.1**

### Property 22: Event retrieval completeness

*For any* notification ID for which events have been recorded, retrieving the event history SHALL return all recorded events in chronological order, each containing a valid timestamp, notification ID, channel, and status.

**Validates: Requirements 10.2, 10.3**

### Property 23: Analytics aggregation consistency

*For any* set of recorded notification events, the aggregate counts (total_sent, total_delivered, total_failed, total_clicked) SHALL equal the number of notifications whose latest status matches each respective category.

**Validates: Requirements 10.4, 10.5**

### Property 24: Notification log query correctness

*For any* set of log entries and any query filter (by notification_id, recipient_id, channel, status, or time range), the query result SHALL contain exactly those entries that match ALL specified filter criteria.

**Validates: Requirements 12.1, 12.2**
