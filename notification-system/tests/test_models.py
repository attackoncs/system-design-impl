"""Unit tests for notification_system.models module."""

import datetime

import notification_system.models


class TestChannelEnum:
    """Tests for Channel enum values."""

    def test_ios_push_value(self):
        assert notification_system.models.Channel.IOS_PUSH.value == "ios_push"

    def test_android_push_value(self):
        assert notification_system.models.Channel.ANDROID_PUSH.value == "android_push"

    def test_sms_value(self):
        assert notification_system.models.Channel.SMS.value == "sms"

    def test_email_value(self):
        assert notification_system.models.Channel.EMAIL.value == "email"

    def test_channel_has_four_members(self):
        assert len(notification_system.models.Channel) == 4


class TestNotificationStatusEnum:
    """Tests for NotificationStatus enum values."""

    def test_created_value(self):
        assert notification_system.models.NotificationStatus.CREATED.value == "created"

    def test_queued_value(self):
        assert notification_system.models.NotificationStatus.QUEUED.value == "queued"

    def test_sending_value(self):
        assert notification_system.models.NotificationStatus.SENDING.value == "sending"

    def test_sent_value(self):
        assert notification_system.models.NotificationStatus.SENT.value == "sent"

    def test_delivered_value(self):
        assert notification_system.models.NotificationStatus.DELIVERED.value == "delivered"

    def test_failed_value(self):
        assert notification_system.models.NotificationStatus.FAILED.value == "failed"

    def test_clicked_value(self):
        assert notification_system.models.NotificationStatus.CLICKED.value == "clicked"

    def test_unsubscribed_value(self):
        assert notification_system.models.NotificationStatus.UNSUBSCRIBED.value == "unsubscribed"

    def test_status_has_eight_members(self):
        assert len(notification_system.models.NotificationStatus) == 8


class TestValidTransitions:
    """Tests for VALID_TRANSITIONS state machine dict."""

    def test_all_statuses_have_entries(self):
        for status in notification_system.models.NotificationStatus:
            assert status in notification_system.models.VALID_TRANSITIONS

    def test_created_transitions(self):
        expected = {
            notification_system.models.NotificationStatus.QUEUED,
            notification_system.models.NotificationStatus.FAILED,
            notification_system.models.NotificationStatus.UNSUBSCRIBED,
        }
        assert notification_system.models.VALID_TRANSITIONS[notification_system.models.NotificationStatus.CREATED] == expected

    def test_queued_transitions(self):
        expected = {notification_system.models.NotificationStatus.SENDING, notification_system.models.NotificationStatus.FAILED}
        assert notification_system.models.VALID_TRANSITIONS[notification_system.models.NotificationStatus.QUEUED] == expected

    def test_sending_transitions(self):
        expected = {notification_system.models.NotificationStatus.SENT, notification_system.models.NotificationStatus.FAILED,
            notification_system.models.NotificationStatus.QUEUED, notification_system.models.NotificationStatus.UNSUBSCRIBED}
        assert notification_system.models.VALID_TRANSITIONS[notification_system.models.NotificationStatus.SENDING] == expected

    def test_sent_transitions(self):
        expected = {notification_system.models.NotificationStatus.DELIVERED, notification_system.models.NotificationStatus.FAILED}
        assert notification_system.models.VALID_TRANSITIONS[notification_system.models.NotificationStatus.SENT] == expected

    def test_delivered_transitions(self):
        expected = {notification_system.models.NotificationStatus.CLICKED}
        assert notification_system.models.VALID_TRANSITIONS[notification_system.models.NotificationStatus.DELIVERED] == expected

    def test_failed_is_terminal(self):
        assert notification_system.models.VALID_TRANSITIONS[notification_system.models.NotificationStatus.FAILED] == set()

    def test_clicked_is_terminal(self):
        assert notification_system.models.VALID_TRANSITIONS[notification_system.models.NotificationStatus.CLICKED] == set()

    def test_unsubscribed_is_terminal(self):
        assert notification_system.models.VALID_TRANSITIONS[notification_system.models.NotificationStatus.UNSUBSCRIBED] == set()


class TestNotificationRequest:
    """Tests for NotificationRequest dataclass."""

    def test_required_fields(self):
        req = notification_system.models.NotificationRequest(recipient_id="user1", channel=notification_system.models.Channel.EMAIL)
        assert req.recipient_id == "user1"
        assert req.channel == notification_system.models.Channel.EMAIL

    def test_default_title_and_body(self):
        req = notification_system.models.NotificationRequest(recipient_id="user1", channel=notification_system.models.Channel.SMS)
        assert req.title == ""
        assert req.body == ""

    def test_default_template_id_is_none(self):
        req = notification_system.models.NotificationRequest(recipient_id="user1", channel=notification_system.models.Channel.SMS)
        assert req.template_id is None

    def test_default_template_params_empty_dict(self):
        req = notification_system.models.NotificationRequest(recipient_id="user1", channel=notification_system.models.Channel.SMS)
        assert req.template_params == {}

    def test_default_metadata_empty_dict(self):
        req = notification_system.models.NotificationRequest(recipient_id="user1", channel=notification_system.models.Channel.SMS)
        assert req.metadata == {}

    def test_frozen_immutability(self):
        req = notification_system.models.NotificationRequest(recipient_id="user1", channel=notification_system.models.Channel.EMAIL)
        try:
            req.recipient_id = "user2"  # type: ignore
            assert False, "Should have raised FrozenInstanceError"
        except AttributeError:
            pass


class TestDeliveryResult:
    """Tests for DeliveryResult dataclass."""

    def test_success_result(self):
        result = notification_system.models.DeliveryResult(success=True, provider_message_id="msg_123")
        assert result.success is True
        assert result.provider_message_id == "msg_123"

    def test_default_error_is_none(self):
        result = notification_system.models.DeliveryResult(success=True)
        assert result.error is None

    def test_default_retryable_is_true(self):
        result = notification_system.models.DeliveryResult(success=False, error="timeout")
        assert result.retryable is True

    def test_failure_result(self):
        result = notification_system.models.DeliveryResult(
            success=False, error="connection refused", retryable=False
        )
        assert result.success is False
        assert result.error == "connection refused"
        assert result.retryable is False


class TestNotificationTask:
    """Tests for NotificationTask dataclass."""

    def test_required_fields(self):
        req = notification_system.models.NotificationRequest(recipient_id="user1", channel=notification_system.models.Channel.EMAIL)
        task = notification_system.models.NotificationTask(
            notification_id="notif_1", request=req, channel=notification_system.models.Channel.EMAIL
        )
        assert task.notification_id == "notif_1"
        assert task.request == req
        assert task.channel == notification_system.models.Channel.EMAIL

    def test_default_attempt_is_zero(self):
        req = notification_system.models.NotificationRequest(recipient_id="user1", channel=notification_system.models.Channel.SMS)
        task = notification_system.models.NotificationTask(
            notification_id="n1", request=req, channel=notification_system.models.Channel.SMS
        )
        assert task.attempt == 0

    def test_default_created_at_is_utc(self):
        req = notification_system.models.NotificationRequest(recipient_id="user1", channel=notification_system.models.Channel.SMS)
        task = notification_system.models.NotificationTask(
            notification_id="n1", request=req, channel=notification_system.models.Channel.SMS
        )
        assert task.created_at.tzinfo == datetime.timezone.utc

    def test_default_scheduled_at_is_none(self):
        req = notification_system.models.NotificationRequest(recipient_id="user1", channel=notification_system.models.Channel.SMS)
        task = notification_system.models.NotificationTask(
            notification_id="n1", request=req, channel=notification_system.models.Channel.SMS
        )
        assert task.scheduled_at is None


class TestNotificationEvent:
    """Tests for NotificationEvent dataclass."""

    def test_required_fields(self):
        event = notification_system.models.NotificationEvent(
            notification_id="n1",
            channel=notification_system.models.Channel.EMAIL,
            status=notification_system.models.NotificationStatus.SENT,
        )
        assert event.notification_id == "n1"
        assert event.channel == notification_system.models.Channel.EMAIL
        assert event.status == notification_system.models.NotificationStatus.SENT

    def test_default_timestamp_is_utc(self):
        event = notification_system.models.NotificationEvent(
            notification_id="n1",
            channel=notification_system.models.Channel.EMAIL,
            status=notification_system.models.NotificationStatus.CREATED,
        )
        assert event.timestamp.tzinfo == datetime.timezone.utc

    def test_default_details_is_none(self):
        event = notification_system.models.NotificationEvent(
            notification_id="n1",
            channel=notification_system.models.Channel.SMS,
            status=notification_system.models.NotificationStatus.QUEUED,
        )
        assert event.details is None


class TestContactInfo:
    """Tests for ContactInfo dataclass."""

    def test_required_user_id(self):
        contact = notification_system.models.ContactInfo(user_id="user1")
        assert contact.user_id == "user1"

    def test_default_email_is_none(self):
        contact = notification_system.models.ContactInfo(user_id="user1")
        assert contact.email is None

    def test_default_phone_is_none(self):
        contact = notification_system.models.ContactInfo(user_id="user1")
        assert contact.phone is None

    def test_default_device_tokens_empty_list(self):
        contact = notification_system.models.ContactInfo(user_id="user1")
        assert contact.device_tokens == []

    def test_with_all_fields(self):
        contact = notification_system.models.ContactInfo(
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
        ts = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
        entry = notification_system.models.LogEntry(
            notification_id="n1",
            recipient_id="user1",
            channel=notification_system.models.Channel.EMAIL,
            content_summary="Hello",
            timestamp=ts,
            status=notification_system.models.NotificationStatus.SENT,
        )
        assert entry.notification_id == "n1"
        assert entry.recipient_id == "user1"
        assert entry.channel == notification_system.models.Channel.EMAIL
        assert entry.content_summary == "Hello"
        assert entry.timestamp == ts
        assert entry.status == notification_system.models.NotificationStatus.SENT

    def test_default_attempt_is_one(self):
        ts = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
        entry = notification_system.models.LogEntry(
            notification_id="n1",
            recipient_id="user1",
            channel=notification_system.models.Channel.SMS,
            content_summary="Test",
            timestamp=ts,
            status=notification_system.models.NotificationStatus.FAILED,
        )
        assert entry.attempt == 1

    def test_default_error_details_is_none(self):
        ts = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
        entry = notification_system.models.LogEntry(
            notification_id="n1",
            recipient_id="user1",
            channel=notification_system.models.Channel.SMS,
            content_summary="Test",
            timestamp=ts,
            status=notification_system.models.NotificationStatus.SENT,
        )
        assert entry.error_details is None
