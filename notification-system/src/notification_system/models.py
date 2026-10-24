"""Data models for the notification system.

Defines enums for channels and notification lifecycle states, the state machine
transition map, and dataclasses for all core data structures used throughout
the system.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
from typing import Optional


class Channel(enum.Enum):
    """Notification delivery channels."""

    IOS_PUSH = "ios_push"
    ANDROID_PUSH = "android_push"
    SMS = "sms"
    EMAIL = "email"


class NotificationStatus(enum.Enum):
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
    NotificationStatus.CREATED: {
        NotificationStatus.QUEUED,
        NotificationStatus.FAILED,
        NotificationStatus.UNSUBSCRIBED,
    },
    NotificationStatus.QUEUED: {
        NotificationStatus.SENDING,
        NotificationStatus.FAILED,
    },
    NotificationStatus.SENDING: {
        NotificationStatus.SENT,
        NotificationStatus.FAILED,
        NotificationStatus.QUEUED,
        NotificationStatus.UNSUBSCRIBED,
    },
    NotificationStatus.SENT: {
        NotificationStatus.DELIVERED,
        NotificationStatus.FAILED,
    },
    NotificationStatus.DELIVERED: {NotificationStatus.CLICKED},
    NotificationStatus.FAILED: set(),
    NotificationStatus.CLICKED: set(),
    NotificationStatus.UNSUBSCRIBED: set(),
}


@dataclasses.dataclass(frozen=True)
class NotificationRequest:
    """A request to send a notification."""

    recipient_id: str
    channel: Channel
    title: str = ""
    body: str = ""
    template_id: Optional[str] = None
    template_params: dict[str, str] = dataclasses.field(default_factory=dict)
    metadata: dict[str, str] = dataclasses.field(default_factory=dict)
    event_id: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class DeliveryResult:
    """Result from a provider delivery attempt."""

    success: bool
    provider_message_id: Optional[str] = None
    error: Optional[str] = None
    retryable: bool = True


@dataclasses.dataclass
class NotificationTask:
    """Internal task representation for queue processing."""

    notification_id: str
    request: NotificationRequest
    channel: Channel
    attempt: int = 0
    created_at: datetime.datetime = dataclasses.field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    scheduled_at: Optional[datetime.datetime] = None
    # Resolved payload and endpoints are captured before accepting the request.
    endpoints: tuple[str, ...] = ()
    completed_endpoints: tuple[str, ...] = ()
    rejected_endpoints: tuple[str, ...] = ()
    endpoint: Optional[str] = None
    receipt: Optional[str] = dataclasses.field(default=None, compare=False, repr=False)


@dataclasses.dataclass(frozen=True)
class NotificationEvent:
    """A lifecycle event for a notification."""

    notification_id: str
    channel: Channel
    status: NotificationStatus
    timestamp: datetime.datetime = dataclasses.field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    details: Optional[str] = None


@dataclasses.dataclass
class ContactInfo:
    """User contact information for notification delivery."""

    user_id: str
    email: Optional[str] = None
    phone: Optional[str] = None
    device_tokens: list[str] = dataclasses.field(default_factory=list)
    # Optional platform registration; unmapped legacy tokens remain usable.
    device_channels: dict[str, Channel] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class LogEntry:
    """A persistent log entry for a notification attempt."""

    notification_id: str
    recipient_id: str
    channel: Channel
    content_summary: str
    timestamp: datetime.datetime
    status: NotificationStatus
    attempt: int = 1
    error_details: Optional[str] = None
