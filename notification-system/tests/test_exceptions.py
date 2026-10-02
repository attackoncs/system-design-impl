"""Unit tests for the custom exception hierarchy.

Tests all 7 exception types: inheritance from NotificationError,
context fields, and descriptive messages.

Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 13.8
"""

import pytest

from notification_system.exceptions import (
    DeliveryError,
    DuplicateNotificationError,
    NotificationError,
    QueueFullError,
    RateLimitExceededError,
    TemplateRenderError,
    ValidationError,
)


class TestNotificationError:
    """Tests for the base NotificationError exception."""

    def test_inherits_from_exception(self):
        assert issubclass(NotificationError, Exception)

    def test_can_be_raised_and_caught(self):
        with pytest.raises(NotificationError):
            raise NotificationError("something went wrong")

    def test_message_is_preserved(self):
        err = NotificationError("test message")
        assert str(err) == "test message"


class TestValidationError:
    """Tests for ValidationError (Requirement 13.2)."""

    def test_inherits_from_notification_error(self):
        assert issubclass(ValidationError, NotificationError)

    def test_field_attribute(self):
        err = ValidationError("Invalid email", field="email")
        assert err.field == "email"

    def test_field_defaults_to_empty_string(self):
        err = ValidationError("Something invalid")
        assert err.field == ""

    def test_message_is_preserved(self):
        err = ValidationError("Missing recipient", field="recipient_id")
        assert str(err) == "Missing recipient"

    def test_caught_as_notification_error(self):
        with pytest.raises(NotificationError):
            raise ValidationError("bad input", field="body")


class TestQueueFullError:
    """Tests for QueueFullError (Requirement 13.3)."""

    def test_inherits_from_notification_error(self):
        assert issubclass(QueueFullError, NotificationError)

    def test_channel_attribute(self):
        err = QueueFullError(channel="email", max_depth=1000)
        assert err.channel == "email"

    def test_max_depth_attribute(self):
        err = QueueFullError(channel="sms", max_depth=500)
        assert err.max_depth == 500

    def test_descriptive_message(self):
        err = QueueFullError(channel="ios_push", max_depth=2000)
        msg = str(err)
        assert "ios_push" in msg
        assert "2000" in msg

    def test_caught_as_notification_error(self):
        with pytest.raises(NotificationError):
            raise QueueFullError(channel="email", max_depth=100)


class TestRateLimitExceededError:
    """Tests for RateLimitExceededError (Requirement 13.4)."""

    def test_inherits_from_notification_error(self):
        assert issubclass(RateLimitExceededError, NotificationError)

    def test_user_id_attribute(self):
        err = RateLimitExceededError(
            user_id="user_123", channel="sms", limit=10, window=3600.0
        )
        assert err.user_id == "user_123"

    def test_channel_attribute(self):
        err = RateLimitExceededError(
            user_id="u1", channel="email", limit=20, window=3600.0
        )
        assert err.channel == "email"

    def test_limit_attribute(self):
        err = RateLimitExceededError(
            user_id="u1", channel="sms", limit=5, window=60.0
        )
        assert err.limit == 5

    def test_window_attribute(self):
        err = RateLimitExceededError(
            user_id="u1", channel="sms", limit=5, window=120.0
        )
        assert err.window == 120.0

    def test_descriptive_message(self):
        err = RateLimitExceededError(
            user_id="user_abc", channel="ios_push", limit=50, window=3600.0
        )
        msg = str(err)
        assert "user_abc" in msg
        assert "ios_push" in msg
        assert "50" in msg
        assert "3600" in msg

    def test_caught_as_notification_error(self):
        with pytest.raises(NotificationError):
            raise RateLimitExceededError(
                user_id="u1", channel="sms", limit=10, window=60.0
            )


class TestDuplicateNotificationError:
    """Tests for DuplicateNotificationError (Requirement 13.5)."""

    def test_inherits_from_notification_error(self):
        assert issubclass(DuplicateNotificationError, NotificationError)

    def test_original_id_attribute(self):
        err = DuplicateNotificationError(original_id="notif_abc123")
        assert err.original_id == "notif_abc123"

    def test_descriptive_message(self):
        err = DuplicateNotificationError(original_id="orig_999")
        msg = str(err)
        assert "orig_999" in msg

    def test_caught_as_notification_error(self):
        with pytest.raises(NotificationError):
            raise DuplicateNotificationError(original_id="x")


class TestTemplateRenderError:
    """Tests for TemplateRenderError (Requirement 13.6)."""

    def test_inherits_from_notification_error(self):
        assert issubclass(TemplateRenderError, NotificationError)

    def test_template_id_attribute(self):
        err = TemplateRenderError(template_id="welcome_email", missing_keys=["name"])
        assert err.template_id == "welcome_email"

    def test_missing_keys_attribute(self):
        err = TemplateRenderError(
            template_id="t1", missing_keys=["first_name", "last_name"]
        )
        assert err.missing_keys == ["first_name", "last_name"]

    def test_descriptive_message(self):
        err = TemplateRenderError(
            template_id="order_confirm", missing_keys=["order_id", "amount"]
        )
        msg = str(err)
        assert "order_confirm" in msg
        assert "order_id" in msg
        assert "amount" in msg

    def test_caught_as_notification_error(self):
        with pytest.raises(NotificationError):
            raise TemplateRenderError(template_id="t", missing_keys=["k"])


class TestDeliveryError:
    """Tests for DeliveryError (Requirement 13.7)."""

    def test_inherits_from_notification_error(self):
        assert issubclass(DeliveryError, NotificationError)

    def test_notification_id_attribute(self):
        err = DeliveryError(
            notification_id="notif_456",
            channel="email",
            reason="SMTP timeout",
        )
        assert err.notification_id == "notif_456"

    def test_channel_attribute(self):
        err = DeliveryError(
            notification_id="n1", channel="sms", reason="Gateway error"
        )
        assert err.channel == "sms"

    def test_reason_attribute(self):
        err = DeliveryError(
            notification_id="n1", channel="email", reason="Connection refused"
        )
        assert err.reason == "Connection refused"

    def test_retryable_defaults_to_true(self):
        err = DeliveryError(
            notification_id="n1", channel="email", reason="Timeout"
        )
        assert err.retryable is True

    def test_retryable_can_be_set_to_false(self):
        err = DeliveryError(
            notification_id="n1",
            channel="ios_push",
            reason="Invalid token",
            retryable=False,
        )
        assert err.retryable is False

    def test_descriptive_message(self):
        err = DeliveryError(
            notification_id="notif_xyz",
            channel="android_push",
            reason="FCM unavailable",
        )
        msg = str(err)
        assert "notif_xyz" in msg
        assert "android_push" in msg
        assert "FCM unavailable" in msg

    def test_caught_as_notification_error(self):
        with pytest.raises(NotificationError):
            raise DeliveryError(
                notification_id="n1", channel="email", reason="fail"
            )
