"""Notification log with pluggable backend for auditing and analytics.

Provides an abstract backend interface and an in-memory implementation.
The NotificationLog facade offers convenience methods for recording
attempts and querying history.
"""

from __future__ import annotations

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
