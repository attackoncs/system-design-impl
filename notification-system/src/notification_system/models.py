"""Data models for the notification system.

Defines enums for channels and notification lifecycle states,
the state machine transition map, and dataclasses for all
core data structures used throughout the system.
"""

from __future__ import annotations

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
