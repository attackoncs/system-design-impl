"""Notification System - An asyncio-based notification delivery library.

Provides a complete notification pipeline with multi-channel delivery,
rate limiting, deduplication, retry with exponential backoff, and
full lifecycle event tracking.
"""

from notification_system.config import (
    NotificationConfig,
    RateLimitConfig,
    RetryConfig,
)
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
from notification_system.log import (
    InMemoryNotificationLog,
    NotificationLog,
    NotificationLogBackend,
)
from notification_system.models import (
    VALID_TRANSITIONS,
    Channel,
    ContactInfo,
    DeliveryResult,
    LogEntry,
    NotificationEvent,
    NotificationRequest,
    NotificationStatus,
    NotificationTask,
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
    # Models
    "Channel",
    "NotificationStatus",
    "VALID_TRANSITIONS",
    "NotificationRequest",
    "DeliveryResult",
    "NotificationTask",
    "NotificationEvent",
    "ContactInfo",
    "LogEntry",
    # Exceptions
    "NotificationError",
    "ValidationError",
    "QueueFullError",
    "RateLimitExceededError",
    "DuplicateNotificationError",
    "TemplateRenderError",
    "DeliveryError",
    # Config
    "RateLimitConfig",
    "RetryConfig",
    "NotificationConfig",
    # Service
    "NotificationService",
    # Queue
    "MessageQueue",
    # Worker
    "Worker",
    "WorkerPool",
    # Provider
    "Provider",
    "SimulatedProvider",
    # Templates
    "NotificationTemplate",
    "TemplateRegistry",
    # Rate Limiter
    "SlidingWindowRateLimiter",
    # Deduplicator
    "Deduplicator",
    # Retry
    "RetryHandler",
    # Settings
    "NotificationSettings",
    # Contacts
    "ContactInfoStore",
    # Tracker
    "EventTracker",
    # Log
    "NotificationLogBackend",
    "InMemoryNotificationLog",
    "NotificationLog",
]
