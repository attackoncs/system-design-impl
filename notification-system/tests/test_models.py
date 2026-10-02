"""Unit tests for notification_system.models module."""

from datetime import datetime, timezone

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


class TestChannelEnum:
    """Tests for Channel enum values."""

    def test_ios_push_value(self):
        assert Channel.IOS_PUSH.value == "ios_push"

    def test_android_push_value(self):
        assert Channel.ANDROID_PUSH.value == "android_push"

    def test_sms_value(self):
        assert Channel.SMS.value == "sms"

    def test_email_value(self):
        assert Channel.EMAIL.value == "email"

    def test_channel_has_four_members(self):
        assert len(Channel) == 4


class TestNotificationStatusEnum:
    """Tests for NotificationStatus enum values."""

    def test_created_value(self):
        assert NotificationStatus.CREATED.value == "created"

    def test_queued_value(self):
        assert NotificationStatus.QUEUED.value == "queued"

    def test_sending_value(self):
        assert NotificationStatus.SENDING.value == "sending"

    def test_sent_value(self):
        assert NotificationStatus.SENT.value == "sent"

    def test_delivered_value(self):
        assert NotificationStatus.DELIVERED.value == "delivered"

    def test_failed_value(self):
        assert NotificationStatus.FAILED.value == "failed"

    def test_clicked_value(self):
        assert NotificationStatus.CLICKED.value == "clicked"

    def test_unsubscribed_value(self):
        assert NotificationStatus.UNSUBSCRIBED.value == "unsubscribed"

    def test_status_has_eight_members(self):
        assert len(NotificationStatus) == 8


class TestValidTransitions:
    """Tests for VALID_TRANSITIONS state machine dict."""

    def test_all_statuses_have_entries(self):
        for status in NotificationStatus:
            assert status in VALID_TRANSITIONS

    def test_created_transitions(self):
        expected = {
            NotificationStatus.QUEUED,
            NotificationStatus.FAILED,
            NotificationStatus.UNSUBSCRIBED,
        }
        assert VALID_TRANSITIONS[NotificationStatus.CREATED] == expected

    def test_queued_transitions(self):
        expected = {NotificationStatus.SENDING, NotificationStatus.FAILED}
        assert VALID_TRANSITIONS[NotificationStatus.QUEUED] == expected

    def test_sending_transitions(self):
        expected = {NotificationStatus.SENT, NotificationStatus.FAILED}
        assert VALID_TRANSITIONS[NotificationStatus.SENDING] == expected

    def test_sent_transitions(self):
        expected = {NotificationStatus.DELIVERED, NotificationStatus.FAILED}
        assert VALID_TRANSITIONS[NotificationStatus.SENT] == expected

    def test_delivered_transitions(self):
        expected = {NotificationStatus.CLICKED}
        assert VALID_TRANSITIONS[NotificationStatus.DELIVERED] == expected

    def test_failed_is_terminal(self):
        assert VALID_TRANSITIONS[NotificationStatus.FAILED] == set()

    def test_clicked_is_terminal(self):
        assert VALID_TRANSITIONS[NotificationStatus.CLICKED] == set()

    def test_unsubscribed_is_terminal(self):
        assert VALID_TRANSITIONS[NotificationStatus.UNSUBSCRIBED] == set()


class TestNotificationRequest:
    """Tests for NotificationRequest dataclass."""

    def test_required_fields(self):
        req = NotificationRequest(recipient_id="user1", channel=Channel.EMAIL)
        assert req.recipient_id == "user1"
        assert req.channel == Channel.EMAIL

    def test_default_title_and_body(self):
        req = NotificationRequest(recipient_id="user1", channel=Channel.SMS)
        assert req.title == ""
        assert req.body == ""

    def test_default_template_id_is_none(self):
        req = NotificationRequest(recipient_id="user1", channel=Channel.SMS)
        assert req.template_id is None

    def test_default_template_params_empty_dict(self):
        req = NotificationRequest(recipient_id="user1", channel=Channel.SMS)
        assert req.template_params == {}

    def test_default_metadata_empty_dict(self):
        req = NotificationRequest(recipient_id="user1", channel=Channel.SMS)
        assert req.metadata == {}

    def test_frozen_immutability(self):
        req = NotificationRequest(recipient_id="user1", channel=Channel.EMAIL)
        try:
            req.recipient_id = "user2"  # type: ignore
            assert False, "Should have raised FrozenInstanceError"
        except AttributeError:
            pass


class TestDeliveryResult:
    """Tests for DeliveryResult dataclass."""

    def test_success_result(self):
        result = DeliveryResult(success=True, provider_message_id="msg_123")
        assert result.success is True
        assert result.provider_message_id == "msg_123"

    def test_default_error_is_none(self):
        result = DeliveryResult(success=True)
        assert result.error is None

    def test_default_retryable_is_true(self):
        result = DeliveryResult(success=False, error="timeout")
        assert result.retryable is True

    def test_failure_result(self):
        result = DeliveryResult(success=False, error="connection refused", retryable=False)
        assert result.success is False
        assert result.error == "connection refused"
        assert result.retryable is False


class TestNotificationTask:
    """Tests for NotificationTask dataclass."""

    def test_required_fields(self):
        req = NotificationRequest(recipient_id="user1", channel=Channel.EMAIL)
        task = NotificationTask(
            notification_id="notif_1", request=req, channel=Channel.EMAIL
        )
        assert task.notification_id == "notif_1"
        assert task.request == req
        assert task.channel == Channel.EMAIL

    def test_default_attempt_is_zero(self):
        req = NotificationRequest(recipient_id="user1", channel=Channel.SMS)
        task = NotificationTask(notification_id="n1", request=req, channel=Channel.SMS)
        assert task.attempt == 0

    def test_default_created_at_is_utc(self):
        req = NotificationRequest(recipient_id="user1", channel=Channel.SMS)
        task = NotificationTask(notification_id="n1", request=req, channel=Channel.SMS)
        assert task.created_at.tzinfo == timezone.utc

    def test_default_scheduled_at_is_none(self):
        req = NotificationRequest(recipient_id="user1", channel=Channel.SMS)
        task = NotificationTask(notification_id="n1", request=req, channel=Channel.SMS)
        assert task.scheduled_at is None


class TestNotificationEvent:
    """Tests for NotificationEvent dataclass."""

    def test_required_fields(self):
        event = NotificationEvent(
            notification_id="n1",
            channel=Channel.EMAIL,
            status=NotificationStatus.SENT,
        )
        assert event.notification_id == "n1"
        assert event.channel == Channel.EMAIL
        assert event.status == NotificationStatus.SENT

    def test_default_timestamp_is_utc(self):
        event = NotificationEvent(
            notification_id="n1",
            channel=Channel.EMAIL,
            status=NotificationStatus.CREATED,
        )
        assert event.timestamp.tzinfo == timezone.utc

    def test_default_details_is_none(self):
        event = NotificationEvent(
            notification_id="n1",
            channel=Channel.SMS,
            status=NotificationStatus.QUEUED,
        )
        assert event.details is None


class TestContactInfo:
    """Tests for ContactInfo dataclass."""

    def test_required_user_id(self):
        contact = ContactInfo(user_id="user1")
        assert contact.user_id == "user1"

    def test_default_email_is_none(self):
        contact = ContactInfo(user_id="user1")
        assert contact.email is None

    def test_default_phone_is_none(self):
        contact = ContactInfo(user_id="user1")
        assert contact.phone is None

    def test_default_device_tokens_empty_list(self):
        contact = ContactInfo(user_id="user1")
        assert contact.device_tokens == []

    def test_with_all_fields(self):
        contact = ContactInfo(
            user_id="user1",
            email="user@example.com",
            phone="+1234567890",
            device_tokens=["token_a", "token_b"],
        )
        assert contact.email == "user@example.com"
        assert contact.phone == "+1234567890"
        assert contact.device_tokens == ["token_a", "token_b"]


class TestLogEntry:
    """Tests for LogEntry dataclass."""

    def test_required_fields(self):
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        entry = LogEntry(
            notification_id="n1",
            recipient_id="user1",
            channel=Channel.EMAIL,
            content_summary="Hello",
            timestamp=ts,
            status=NotificationStatus.SENT,
        )
        assert entry.notification_id == "n1"
        assert entry.recipient_id == "user1"
        assert entry.channel == Channel.EMAIL
        assert entry.content_summary == "Hello"
        assert entry.timestamp == ts
        assert entry.status == NotificationStatus.SENT

    def test_default_attempt_is_one(self):
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        entry = LogEntry(
            notification_id="n1",
            recipient_id="user1",
            channel=Channel.SMS,
            content_summary="Test",
            timestamp=ts,
            status=NotificationStatus.FAILED,
        )
        assert entry.attempt == 1

    def test_default_error_details_is_none(self):
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        entry = LogEntry(
            notification_id="n1",
            recipient_id="user1",
            channel=Channel.SMS,
            content_summary="Test",
            timestamp=ts,
            status=NotificationStatus.SENT,
        )
        assert entry.error_details is None
