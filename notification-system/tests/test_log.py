"""Unit tests for notification log module.

Tests InMemoryNotificationLog: append, query by various filters
(notification_id, recipient_id, channel, status, time range), cleanup/retention.
Tests NotificationLog facade: record_attempt creates LogEntry, query delegates to backend.

Requirements: 12.1, 12.2, 12.3, 12.4
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from notification_system.log import InMemoryNotificationLog, NotificationLog
from notification_system.models import Channel, LogEntry, NotificationStatus


def _make_entry(
    notification_id: str = "n1",
    recipient_id: str = "r1",
    channel: Channel = Channel.EMAIL,
    status: NotificationStatus = NotificationStatus.SENT,
    timestamp: Optional[datetime] = None,
    content_summary: str = "Hello",
    attempt: int = 1,
    error_details: Optional[str] = None,
) -> LogEntry:
    """Helper to create a LogEntry with sensible defaults."""
    return LogEntry(
        notification_id=notification_id,
        recipient_id=recipient_id,
        channel=channel,
        content_summary=content_summary,
        timestamp=timestamp or datetime.now(timezone.utc),
        status=status,
        attempt=attempt,
        error_details=error_details,
    )


class TestInMemoryNotificationLogAppend:
    """Tests for InMemoryNotificationLog.append."""

    def test_append_single_entry(self):
        log = InMemoryNotificationLog()
        entry = _make_entry()
        log.append(entry)
        results = log.query()
        assert len(results) == 1
        assert results[0] is entry

    def test_append_multiple_entries(self):
        log = InMemoryNotificationLog()
        entries = [_make_entry(notification_id=f"n{i}") for i in range(5)]
        for e in entries:
            log.append(e)
        results = log.query()
        assert len(results) == 5

    def test_append_preserves_order(self):
        log = InMemoryNotificationLog()
        e1 = _make_entry(notification_id="first")
        e2 = _make_entry(notification_id="second")
        log.append(e1)
        log.append(e2)
        results = log.query()
        assert results[0].notification_id == "first"
        assert results[1].notification_id == "second"


class TestInMemoryNotificationLogQueryFilters:
    """Tests for InMemoryNotificationLog.query with various filters."""

    def test_query_no_filters_returns_all(self):
        log = InMemoryNotificationLog()
        log.append(_make_entry(notification_id="n1"))
        log.append(_make_entry(notification_id="n2"))
        assert len(log.query()) == 2

    def test_query_by_notification_id(self):
        log = InMemoryNotificationLog()
        log.append(_make_entry(notification_id="target"))
        log.append(_make_entry(notification_id="other"))
        results = log.query(notification_id="target")
        assert len(results) == 1
        assert results[0].notification_id == "target"

    def test_query_by_recipient_id(self):
        log = InMemoryNotificationLog()
        log.append(_make_entry(recipient_id="user_a"))
        log.append(_make_entry(recipient_id="user_b"))
        log.append(_make_entry(recipient_id="user_a"))
        results = log.query(recipient_id="user_a")
        assert len(results) == 2
        assert all(r.recipient_id == "user_a" for r in results)

    def test_query_by_channel(self):
        log = InMemoryNotificationLog()
        log.append(_make_entry(channel=Channel.EMAIL))
        log.append(_make_entry(channel=Channel.SMS))
        log.append(_make_entry(channel=Channel.EMAIL))
        results = log.query(channel=Channel.EMAIL)
        assert len(results) == 2
        assert all(r.channel == Channel.EMAIL for r in results)

    def test_query_by_status(self):
        log = InMemoryNotificationLog()
        log.append(_make_entry(status=NotificationStatus.SENT))
        log.append(_make_entry(status=NotificationStatus.FAILED))
        log.append(_make_entry(status=NotificationStatus.SENT))
        results = log.query(status=NotificationStatus.FAILED)
        assert len(results) == 1
        assert results[0].status == NotificationStatus.FAILED

    def test_query_by_start_time(self):
        log = InMemoryNotificationLog()
        t1 = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t3 = datetime(2024, 1, 1, 14, 0, 0, tzinfo=timezone.utc)
        log.append(_make_entry(notification_id="early", timestamp=t1))
        log.append(_make_entry(notification_id="mid", timestamp=t2))
        log.append(_make_entry(notification_id="late", timestamp=t3))
        # start_time filters out entries before it
        results = log.query(start_time=datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc))
        assert len(results) == 2
        ids = [r.notification_id for r in results]
        assert "mid" in ids
        assert "late" in ids

    def test_query_by_end_time(self):
        log = InMemoryNotificationLog()
        t1 = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t3 = datetime(2024, 1, 1, 14, 0, 0, tzinfo=timezone.utc)
        log.append(_make_entry(notification_id="early", timestamp=t1))
        log.append(_make_entry(notification_id="mid", timestamp=t2))
        log.append(_make_entry(notification_id="late", timestamp=t3))
        # end_time filters out entries after it
        results = log.query(end_time=datetime(2024, 1, 1, 13, 0, 0, tzinfo=timezone.utc))
        assert len(results) == 2
        ids = [r.notification_id for r in results]
        assert "early" in ids
        assert "mid" in ids

    def test_query_by_time_range(self):
        log = InMemoryNotificationLog()
        t1 = datetime(2024, 1, 1, 8, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t3 = datetime(2024, 1, 1, 16, 0, 0, tzinfo=timezone.utc)
        log.append(_make_entry(notification_id="early", timestamp=t1))
        log.append(_make_entry(notification_id="mid", timestamp=t2))
        log.append(_make_entry(notification_id="late", timestamp=t3))
        results = log.query(
            start_time=datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
            end_time=datetime(2024, 1, 1, 14, 0, 0, tzinfo=timezone.utc),
        )
        assert len(results) == 1
        assert results[0].notification_id == "mid"

    def test_query_combined_filters(self):
        log = InMemoryNotificationLog()
        log.append(_make_entry(recipient_id="u1", channel=Channel.EMAIL, status=NotificationStatus.SENT))
        log.append(_make_entry(recipient_id="u1", channel=Channel.SMS, status=NotificationStatus.SENT))
        log.append(_make_entry(recipient_id="u2", channel=Channel.EMAIL, status=NotificationStatus.SENT))
        log.append(_make_entry(recipient_id="u1", channel=Channel.EMAIL, status=NotificationStatus.FAILED))
        results = log.query(recipient_id="u1", channel=Channel.EMAIL, status=NotificationStatus.SENT)
        assert len(results) == 1
        assert results[0].recipient_id == "u1"
        assert results[0].channel == Channel.EMAIL
        assert results[0].status == NotificationStatus.SENT

    def test_query_no_match_returns_empty(self):
        log = InMemoryNotificationLog()
        log.append(_make_entry(notification_id="n1"))
        results = log.query(notification_id="nonexistent")
        assert results == []


class TestInMemoryNotificationLogCleanup:
    """Tests for InMemoryNotificationLog.cleanup (retention)."""

    def test_cleanup_removes_old_entries(self):
        log = InMemoryNotificationLog()
        old_time = datetime.now(timezone.utc) - timedelta(seconds=100)
        recent_time = datetime.now(timezone.utc) - timedelta(seconds=10)
        log.append(_make_entry(notification_id="old", timestamp=old_time))
        log.append(_make_entry(notification_id="recent", timestamp=recent_time))
        removed = log.cleanup(retention_seconds=50)
        assert removed == 1
        remaining = log.query()
        assert len(remaining) == 1
        assert remaining[0].notification_id == "recent"

    def test_cleanup_returns_count_removed(self):
        log = InMemoryNotificationLog()
        old_time = datetime.now(timezone.utc) - timedelta(seconds=200)
        for i in range(3):
            log.append(_make_entry(notification_id=f"old_{i}", timestamp=old_time))
        log.append(_make_entry(notification_id="recent"))
        removed = log.cleanup(retention_seconds=100)
        assert removed == 3

    def test_cleanup_removes_nothing_when_all_recent(self):
        log = InMemoryNotificationLog()
        log.append(_make_entry(notification_id="n1"))
        log.append(_make_entry(notification_id="n2"))
        removed = log.cleanup(retention_seconds=3600)
        assert removed == 0
        assert len(log.query()) == 2

    def test_cleanup_removes_all_when_all_old(self):
        log = InMemoryNotificationLog()
        old_time = datetime.now(timezone.utc) - timedelta(seconds=500)
        log.append(_make_entry(notification_id="n1", timestamp=old_time))
        log.append(_make_entry(notification_id="n2", timestamp=old_time))
        removed = log.cleanup(retention_seconds=100)
        assert removed == 2
        assert len(log.query()) == 0

    def test_cleanup_with_zero_retention_removes_all(self):
        log = InMemoryNotificationLog()
        # Even entries created "now" will be older than 0 seconds retention
        # due to the slight time difference between creation and cleanup
        old_time = datetime.now(timezone.utc) - timedelta(seconds=1)
        log.append(_make_entry(timestamp=old_time))
        removed = log.cleanup(retention_seconds=0)
        assert removed == 1


class TestNotificationLogFacade:
    """Tests for NotificationLog facade."""

    def test_record_attempt_creates_log_entry(self):
        facade = NotificationLog()
        facade.record_attempt(
            notification_id="n1",
            recipient_id="r1",
            channel=Channel.EMAIL,
            content_summary="Test email",
            status=NotificationStatus.SENT,
            attempt=1,
        )
        results = facade.query()
        assert len(results) == 1
        entry = results[0]
        assert entry.notification_id == "n1"
        assert entry.recipient_id == "r1"
        assert entry.channel == Channel.EMAIL
        assert entry.content_summary == "Test email"
        assert entry.status == NotificationStatus.SENT
        assert entry.attempt == 1
        assert entry.error_details is None

    def test_record_attempt_with_error_details(self):
        facade = NotificationLog()
        facade.record_attempt(
            notification_id="n2",
            recipient_id="r2",
            channel=Channel.SMS,
            content_summary="Test SMS",
            status=NotificationStatus.FAILED,
            attempt=3,
            error_details="Provider timeout",
        )
        results = facade.query()
        assert len(results) == 1
        entry = results[0]
        assert entry.status == NotificationStatus.FAILED
        assert entry.attempt == 3
        assert entry.error_details == "Provider timeout"

    def test_record_attempt_sets_timestamp(self):
        facade = NotificationLog()
        before = datetime.now(timezone.utc)
        facade.record_attempt(
            notification_id="n1",
            recipient_id="r1",
            channel=Channel.EMAIL,
            content_summary="Test",
            status=NotificationStatus.SENT,
        )
        after = datetime.now(timezone.utc)
        entry = facade.query()[0]
        assert before <= entry.timestamp <= after

    def test_query_delegates_to_backend(self):
        facade = NotificationLog()
        facade.record_attempt(
            notification_id="n1",
            recipient_id="r1",
            channel=Channel.EMAIL,
            content_summary="Email 1",
            status=NotificationStatus.SENT,
        )
        facade.record_attempt(
            notification_id="n2",
            recipient_id="r2",
            channel=Channel.SMS,
            content_summary="SMS 1",
            status=NotificationStatus.FAILED,
        )
        # Query with filter should delegate properly
        results = facade.query(channel=Channel.SMS)
        assert len(results) == 1
        assert results[0].notification_id == "n2"

    def test_cleanup_delegates_to_backend(self):
        facade = NotificationLog()
        facade.record_attempt(
            notification_id="n1",
            recipient_id="r1",
            channel=Channel.EMAIL,
            content_summary="Old",
            status=NotificationStatus.SENT,
        )
        # Cleanup with very large retention should remove nothing
        removed = facade.cleanup(retention_seconds=3600)
        assert removed == 0

    def test_uses_default_inmemory_backend(self):
        facade = NotificationLog()
        # Should work without explicit backend
        facade.record_attempt(
            notification_id="n1",
            recipient_id="r1",
            channel=Channel.EMAIL,
            content_summary="Test",
            status=NotificationStatus.SENT,
        )
        assert len(facade.query()) == 1

    def test_uses_custom_backend(self):
        custom_backend = InMemoryNotificationLog()
        facade = NotificationLog(backend=custom_backend)
        facade.record_attempt(
            notification_id="n1",
            recipient_id="r1",
            channel=Channel.EMAIL,
            content_summary="Test",
            status=NotificationStatus.SENT,
        )
        # Verify the entry went to the custom backend
        assert len(custom_backend.query()) == 1
