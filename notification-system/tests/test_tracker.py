"""Unit tests for EventTracker.

Tests event recording, history retrieval, get_current_status,
get_aggregate_counts, get_channel_stats, invalid state transition
raises ValueError, valid transitions accepted.

Requirements: 10.1, 10.2, 10.3, 10.4, 10.5
"""

from datetime import datetime, timezone

import pytest

from notification_system.models import Channel, NotificationStatus, VALID_TRANSITIONS
from notification_system.tracker import EventTracker


def _fixed_clock(dt: datetime):
    """Return a clock callable that always returns the given datetime."""
    return lambda: dt


class TestEventRecording:
    """Tests for recording lifecycle events."""

    def test_record_initial_event(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CREATED)
        history = tracker.get_history("n1")
        assert len(history) == 1
        assert history[0].notification_id == "n1"
        assert history[0].channel == Channel.EMAIL
        assert history[0].status == NotificationStatus.CREATED

    def test_record_stores_details(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.SMS, NotificationStatus.CREATED, details="initial creation")
        history = tracker.get_history("n1")
        assert history[0].details == "initial creation"

    def test_record_uses_custom_clock(self):
        fixed_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        tracker = EventTracker(clock=_fixed_clock(fixed_time))
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CREATED)
        history = tracker.get_history("n1")
        assert history[0].timestamp == fixed_time

    def test_record_multiple_events_for_same_notification(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CREATED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.QUEUED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.SENDING)
        history = tracker.get_history("n1")
        assert len(history) == 3
        assert history[0].status == NotificationStatus.CREATED
        assert history[1].status == NotificationStatus.QUEUED
        assert history[2].status == NotificationStatus.SENDING


class TestHistoryRetrieval:
    """Tests for get_history."""

    def test_get_history_empty_for_unknown_notification(self):
        tracker = EventTracker()
        assert tracker.get_history("nonexistent") == []

    def test_get_history_returns_chronological_order(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.IOS_PUSH, NotificationStatus.CREATED)
        tracker.record("n1", Channel.IOS_PUSH, NotificationStatus.QUEUED)
        tracker.record("n1", Channel.IOS_PUSH, NotificationStatus.SENDING)
        tracker.record("n1", Channel.IOS_PUSH, NotificationStatus.SENT)
        history = tracker.get_history("n1")
        statuses = [e.status for e in history]
        assert statuses == [
            NotificationStatus.CREATED,
            NotificationStatus.QUEUED,
            NotificationStatus.SENDING,
            NotificationStatus.SENT,
        ]

    def test_get_history_returns_copy(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CREATED)
        history1 = tracker.get_history("n1")
        history2 = tracker.get_history("n1")
        assert history1 is not history2
        assert history1 == history2


class TestGetCurrentStatus:
    """Tests for get_current_status."""

    def test_returns_none_for_unknown_notification(self):
        tracker = EventTracker()
        assert tracker.get_current_status("unknown") is None

    def test_returns_latest_status(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CREATED)
        assert tracker.get_current_status("n1") == NotificationStatus.CREATED
        tracker.record("n1", Channel.EMAIL, NotificationStatus.QUEUED)
        assert tracker.get_current_status("n1") == NotificationStatus.QUEUED
        tracker.record("n1", Channel.EMAIL, NotificationStatus.SENDING)
        assert tracker.get_current_status("n1") == NotificationStatus.SENDING

    def test_returns_terminal_status(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.SMS, NotificationStatus.CREATED)
        tracker.record("n1", Channel.SMS, NotificationStatus.FAILED)
        assert tracker.get_current_status("n1") == NotificationStatus.FAILED


class TestGetAggregateCounts:
    """Tests for get_aggregate_counts."""

    def test_empty_tracker_returns_zero_counts(self):
        tracker = EventTracker()
        counts = tracker.get_aggregate_counts()
        assert counts == {
            "total_sent": 0,
            "total_delivered": 0,
            "total_failed": 0,
            "total_clicked": 0,
        }

    def test_counts_by_final_status(self):
        tracker = EventTracker()
        # Notification ending in SENT
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CREATED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.QUEUED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.SENDING)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.SENT)
        # Notification ending in DELIVERED
        tracker.record("n2", Channel.SMS, NotificationStatus.CREATED)
        tracker.record("n2", Channel.SMS, NotificationStatus.QUEUED)
        tracker.record("n2", Channel.SMS, NotificationStatus.SENDING)
        tracker.record("n2", Channel.SMS, NotificationStatus.SENT)
        tracker.record("n2", Channel.SMS, NotificationStatus.DELIVERED)
        # Notification ending in FAILED
        tracker.record("n3", Channel.IOS_PUSH, NotificationStatus.CREATED)
        tracker.record("n3", Channel.IOS_PUSH, NotificationStatus.FAILED)
        # Notification ending in CLICKED
        tracker.record("n4", Channel.ANDROID_PUSH, NotificationStatus.CREATED)
        tracker.record("n4", Channel.ANDROID_PUSH, NotificationStatus.QUEUED)
        tracker.record("n4", Channel.ANDROID_PUSH, NotificationStatus.SENDING)
        tracker.record("n4", Channel.ANDROID_PUSH, NotificationStatus.SENT)
        tracker.record("n4", Channel.ANDROID_PUSH, NotificationStatus.DELIVERED)
        tracker.record("n4", Channel.ANDROID_PUSH, NotificationStatus.CLICKED)

        counts = tracker.get_aggregate_counts()
        assert counts["total_sent"] == 1
        assert counts["total_delivered"] == 1
        assert counts["total_failed"] == 1
        assert counts["total_clicked"] == 1

    def test_counts_with_time_range_filter(self):
        t1 = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t3 = datetime(2024, 1, 1, 14, 0, 0, tzinfo=timezone.utc)

        times = iter([t1, t2, t3])
        tracker = EventTracker(clock=lambda: next(times))

        tracker.record("n1", Channel.EMAIL, NotificationStatus.SENT)
        tracker.record("n2", Channel.EMAIL, NotificationStatus.DELIVERED)
        tracker.record("n3", Channel.EMAIL, NotificationStatus.FAILED)

        # Filter: only events with last timestamp between 11:00 and 13:00
        start = datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 1, 13, 0, 0, tzinfo=timezone.utc)
        counts = tracker.get_aggregate_counts(start_time=start, end_time=end)
        assert counts["total_delivered"] == 1
        assert counts["total_sent"] == 0
        assert counts["total_failed"] == 0


class TestGetChannelStats:
    """Tests for get_channel_stats."""

    def test_empty_tracker_returns_zero_stats(self):
        tracker = EventTracker()
        stats = tracker.get_channel_stats(Channel.EMAIL)
        assert stats == {"total": 0, "success_rate": 0.0, "failure_rate": 0.0}

    def test_all_successful_deliveries(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CREATED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.QUEUED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.SENDING)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.SENT)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.DELIVERED)

        tracker.record("n2", Channel.EMAIL, NotificationStatus.CREATED)
        tracker.record("n2", Channel.EMAIL, NotificationStatus.QUEUED)
        tracker.record("n2", Channel.EMAIL, NotificationStatus.SENDING)
        tracker.record("n2", Channel.EMAIL, NotificationStatus.SENT)

        stats = tracker.get_channel_stats(Channel.EMAIL)
        assert stats["total"] == 2
        assert stats["success_rate"] == 1.0
        assert stats["failure_rate"] == 0.0

    def test_mixed_success_and_failure(self):
        tracker = EventTracker()
        # Successful
        tracker.record("n1", Channel.SMS, NotificationStatus.CREATED)
        tracker.record("n1", Channel.SMS, NotificationStatus.QUEUED)
        tracker.record("n1", Channel.SMS, NotificationStatus.SENDING)
        tracker.record("n1", Channel.SMS, NotificationStatus.SENT)
        # Failed
        tracker.record("n2", Channel.SMS, NotificationStatus.CREATED)
        tracker.record("n2", Channel.SMS, NotificationStatus.FAILED)

        stats = tracker.get_channel_stats(Channel.SMS)
        assert stats["total"] == 2
        assert stats["success_rate"] == 0.5
        assert stats["failure_rate"] == 0.5

    def test_only_counts_matching_channel(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CREATED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.QUEUED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.SENDING)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.SENT)

        tracker.record("n2", Channel.SMS, NotificationStatus.CREATED)
        tracker.record("n2", Channel.SMS, NotificationStatus.FAILED)

        email_stats = tracker.get_channel_stats(Channel.EMAIL)
        assert email_stats["total"] == 1
        assert email_stats["success_rate"] == 1.0

        sms_stats = tracker.get_channel_stats(Channel.SMS)
        assert sms_stats["total"] == 1
        assert sms_stats["failure_rate"] == 1.0


class TestInvalidStateTransition:
    """Tests that invalid state transitions raise ValueError."""

    def test_failed_to_sent_raises_valueerror(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CREATED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.FAILED)
        with pytest.raises(ValueError, match="Invalid state transition"):
            tracker.record("n1", Channel.EMAIL, NotificationStatus.SENT)

    def test_clicked_to_delivered_raises_valueerror(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CREATED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.QUEUED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.SENDING)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.SENT)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.DELIVERED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CLICKED)
        with pytest.raises(ValueError, match="Invalid state transition"):
            tracker.record("n1", Channel.EMAIL, NotificationStatus.DELIVERED)

    def test_created_to_sent_raises_valueerror(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.SMS, NotificationStatus.CREATED)
        with pytest.raises(ValueError, match="Invalid state transition"):
            tracker.record("n1", Channel.SMS, NotificationStatus.SENT)

    def test_queued_to_delivered_raises_valueerror(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.IOS_PUSH, NotificationStatus.CREATED)
        tracker.record("n1", Channel.IOS_PUSH, NotificationStatus.QUEUED)
        with pytest.raises(ValueError, match="Invalid state transition"):
            tracker.record("n1", Channel.IOS_PUSH, NotificationStatus.DELIVERED)

    def test_error_message_includes_notification_id(self):
        tracker = EventTracker()
        tracker.record("my-notif-123", Channel.EMAIL, NotificationStatus.CREATED)
        tracker.record("my-notif-123", Channel.EMAIL, NotificationStatus.FAILED)
        with pytest.raises(ValueError, match="my-notif-123"):
            tracker.record("my-notif-123", Channel.EMAIL, NotificationStatus.QUEUED)


class TestValidTransitions:
    """Tests that all valid transitions are accepted without error."""

    def test_created_to_queued(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CREATED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.QUEUED)
        assert tracker.get_current_status("n1") == NotificationStatus.QUEUED

    def test_created_to_failed(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CREATED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.FAILED)
        assert tracker.get_current_status("n1") == NotificationStatus.FAILED

    def test_created_to_unsubscribed(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CREATED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.UNSUBSCRIBED)
        assert tracker.get_current_status("n1") == NotificationStatus.UNSUBSCRIBED

    def test_queued_to_sending(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.SMS, NotificationStatus.CREATED)
        tracker.record("n1", Channel.SMS, NotificationStatus.QUEUED)
        tracker.record("n1", Channel.SMS, NotificationStatus.SENDING)
        assert tracker.get_current_status("n1") == NotificationStatus.SENDING

    def test_queued_to_failed(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.SMS, NotificationStatus.CREATED)
        tracker.record("n1", Channel.SMS, NotificationStatus.QUEUED)
        tracker.record("n1", Channel.SMS, NotificationStatus.FAILED)
        assert tracker.get_current_status("n1") == NotificationStatus.FAILED

    def test_sending_to_sent(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.IOS_PUSH, NotificationStatus.CREATED)
        tracker.record("n1", Channel.IOS_PUSH, NotificationStatus.QUEUED)
        tracker.record("n1", Channel.IOS_PUSH, NotificationStatus.SENDING)
        tracker.record("n1", Channel.IOS_PUSH, NotificationStatus.SENT)
        assert tracker.get_current_status("n1") == NotificationStatus.SENT

    def test_sending_to_failed(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.ANDROID_PUSH, NotificationStatus.CREATED)
        tracker.record("n1", Channel.ANDROID_PUSH, NotificationStatus.QUEUED)
        tracker.record("n1", Channel.ANDROID_PUSH, NotificationStatus.SENDING)
        tracker.record("n1", Channel.ANDROID_PUSH, NotificationStatus.FAILED)
        assert tracker.get_current_status("n1") == NotificationStatus.FAILED

    def test_sent_to_delivered(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CREATED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.QUEUED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.SENDING)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.SENT)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.DELIVERED)
        assert tracker.get_current_status("n1") == NotificationStatus.DELIVERED

    def test_sent_to_failed(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CREATED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.QUEUED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.SENDING)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.SENT)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.FAILED)
        assert tracker.get_current_status("n1") == NotificationStatus.FAILED

    def test_delivered_to_clicked(self):
        tracker = EventTracker()
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CREATED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.QUEUED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.SENDING)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.SENT)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.DELIVERED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CLICKED)
        assert tracker.get_current_status("n1") == NotificationStatus.CLICKED

    def test_full_happy_path(self):
        """Test the complete lifecycle: CREATED -> QUEUED -> SENDING -> SENT -> DELIVERED -> CLICKED."""
        tracker = EventTracker()
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CREATED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.QUEUED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.SENDING)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.SENT)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.DELIVERED)
        tracker.record("n1", Channel.EMAIL, NotificationStatus.CLICKED)
        history = tracker.get_history("n1")
        assert len(history) == 6
        assert tracker.get_current_status("n1") == NotificationStatus.CLICKED
