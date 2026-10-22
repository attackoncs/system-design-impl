"""Unit tests for the Deduplicator module."""

import notification_system.dedup
import notification_system.exceptions
import notification_system.models
import pytest


class TestComputeKey:
    """Tests for compute_key method."""

    def test_returns_sha256_hex_digest(self):
        dedup = notification_system.dedup.Deduplicator()
        key = dedup.compute_key("user1", notification_system.models.Channel.EMAIL, "Hello World")
        # SHA-256 hex digest is 64 characters
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)

    def test_same_inputs_produce_same_key(self):
        dedup = notification_system.dedup.Deduplicator()
        key1 = dedup.compute_key("user1", notification_system.models.Channel.EMAIL, "Hello")
        key2 = dedup.compute_key("user1", notification_system.models.Channel.EMAIL, "Hello")
        assert key1 == key2

    def test_different_recipient_produces_different_key(self):
        dedup = notification_system.dedup.Deduplicator()
        key1 = dedup.compute_key("user1", notification_system.models.Channel.EMAIL, "Hello")
        key2 = dedup.compute_key("user2", notification_system.models.Channel.EMAIL, "Hello")
        assert key1 != key2

    def test_different_channel_produces_different_key(self):
        dedup = notification_system.dedup.Deduplicator()
        key1 = dedup.compute_key("user1", notification_system.models.Channel.EMAIL, "Hello")
        key2 = dedup.compute_key("user1", notification_system.models.Channel.SMS, "Hello")
        assert key1 != key2

    def test_different_content_produces_different_key(self):
        dedup = notification_system.dedup.Deduplicator()
        key1 = dedup.compute_key("user1", notification_system.models.Channel.EMAIL, "Hello")
        key2 = dedup.compute_key("user1", notification_system.models.Channel.EMAIL, "Goodbye")
        assert key1 != key2


class TestCheckAndRecord:
    """Tests for check and record methods."""

    def test_check_returns_none_for_unknown_key(self):
        dedup = notification_system.dedup.Deduplicator()
        key = dedup.compute_key("user1", notification_system.models.Channel.EMAIL, "Hello")
        assert dedup.check(key) is None

    def test_check_returns_notification_id_after_record(self):
        dedup = notification_system.dedup.Deduplicator()
        key = dedup.compute_key("user1", notification_system.models.Channel.EMAIL, "Hello")
        dedup.record(key, "notif-123")
        assert dedup.check(key) == "notif-123"

    def test_check_returns_none_after_ttl_expires(self):
        current_time = 0.0

        def clock():
            return current_time

        dedup = notification_system.dedup.Deduplicator(window_seconds=10.0, clock=clock)
        key = dedup.compute_key("user1", notification_system.models.Channel.EMAIL, "Hello")
        dedup.record(key, "notif-123")

        # Advance time past the TTL window
        current_time = 11.0
        assert dedup.check(key) is None

    def test_check_returns_id_within_ttl_window(self):
        current_time = 0.0

        def clock():
            return current_time

        dedup = notification_system.dedup.Deduplicator(window_seconds=10.0, clock=clock)
        key = dedup.compute_key("user1", notification_system.models.Channel.EMAIL, "Hello")
        dedup.record(key, "notif-123")

        # Advance time but stay within window
        current_time = 9.0
        assert dedup.check(key) == "notif-123"


class TestCheckAndRecordConvenience:
    """Tests for check_and_record convenience method."""

    def test_first_notification_passes(self):
        dedup = notification_system.dedup.Deduplicator()
        # Should not raise
        dedup.check_and_record("user1", notification_system.models.Channel.EMAIL, "Hello", "notif-1")

    def test_duplicate_raises_error(self):
        dedup = notification_system.dedup.Deduplicator()
        dedup.check_and_record("user1", notification_system.models.Channel.EMAIL, "Hello", "notif-1")

        with pytest.raises(notification_system.exceptions.DuplicateNotificationError) as exc_info:
            dedup.check_and_record("user1", notification_system.models.Channel.EMAIL, "Hello", "notif-2")

        assert exc_info.value.original_id == "notif-1"

    def test_different_content_does_not_duplicate(self):
        dedup = notification_system.dedup.Deduplicator()
        dedup.check_and_record("user1", notification_system.models.Channel.EMAIL, "Hello", "notif-1")
        # Different content should not raise
        dedup.check_and_record("user1", notification_system.models.Channel.EMAIL, "Goodbye", "notif-2")

    def test_duplicate_allowed_after_ttl_expires(self):
        current_time = 0.0

        def clock():
            return current_time

        dedup = notification_system.dedup.Deduplicator(window_seconds=10.0, clock=clock)
        dedup.check_and_record("user1", notification_system.models.Channel.EMAIL, "Hello", "notif-1")

        # Advance past TTL
        current_time = 11.0
        # Should not raise - TTL expired
        dedup.check_and_record("user1", notification_system.models.Channel.EMAIL, "Hello", "notif-2")


class TestExpiry:
    """Tests for automatic expiry of old entries."""

    def test_entry_count_reflects_active_entries(self):
        dedup = notification_system.dedup.Deduplicator()
        key1 = dedup.compute_key("user1", notification_system.models.Channel.EMAIL, "Hello")
        key2 = dedup.compute_key("user2", notification_system.models.Channel.SMS, "World")
        dedup.record(key1, "notif-1")
        dedup.record(key2, "notif-2")
        assert dedup.entry_count == 2

    def test_expired_entries_are_cleaned_up(self):
        current_time = 0.0

        def clock():
            return current_time

        dedup = notification_system.dedup.Deduplicator(window_seconds=10.0, clock=clock)
        key = dedup.compute_key("user1", notification_system.models.Channel.EMAIL, "Hello")
        dedup.record(key, "notif-1")
        assert dedup.entry_count == 1

        # Advance past TTL
        current_time = 11.0
        assert dedup.entry_count == 0

    def test_mixed_expiry_only_removes_old(self):
        current_time = 0.0

        def clock():
            return current_time

        dedup = notification_system.dedup.Deduplicator(window_seconds=10.0, clock=clock)
        key1 = dedup.compute_key("user1", notification_system.models.Channel.EMAIL, "Hello")
        dedup.record(key1, "notif-1")

        current_time = 5.0
        key2 = dedup.compute_key("user2", notification_system.models.Channel.SMS, "World")
        dedup.record(key2, "notif-2")

        # Advance to expire first but not second
        current_time = 11.0
        assert dedup.entry_count == 1
        assert dedup.check(key1) is None
        assert dedup.check(key2) == "notif-2"
