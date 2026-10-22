"""Unit tests for the NotificationSettings module."""

import notification_system.models
import notification_system.settings


class TestDefaultPreferences:
    """Tests for default opt-in behavior (Requirement 8.2)."""

    def test_new_user_opted_in_all_channels(self):
        settings = notification_system.settings.NotificationSettings()
        prefs = settings.get_all_preferences("user1")
        for channel in notification_system.models.Channel:
            assert prefs[channel] is True

    def test_is_opted_in_returns_true_for_new_user(self):
        settings = notification_system.settings.NotificationSettings()
        for channel in notification_system.models.Channel:
            assert settings.is_opted_in("user1", channel) is True

    def test_different_users_get_independent_defaults(self):
        settings = notification_system.settings.NotificationSettings()
        settings.opt_out("user1", notification_system.models.Channel.EMAIL)
        # user2 should still be opted in everywhere
        assert settings.is_opted_in("user2", notification_system.models.Channel.EMAIL) is True


class TestOptOut:
    """Tests for opt-out behavior (Requirement 8.3)."""

    def test_opt_out_single_channel(self):
        settings = notification_system.settings.NotificationSettings()
        settings.opt_out("user1", notification_system.models.Channel.SMS)
        assert settings.is_opted_in("user1", notification_system.models.Channel.SMS) is False

    def test_opt_out_does_not_affect_other_channels(self):
        settings = notification_system.settings.NotificationSettings()
        settings.opt_out("user1", notification_system.models.Channel.SMS)
        assert settings.is_opted_in("user1", notification_system.models.Channel.EMAIL) is True
        assert settings.is_opted_in("user1", notification_system.models.Channel.IOS_PUSH) is True
        assert settings.is_opted_in("user1", notification_system.models.Channel.ANDROID_PUSH) is True

    def test_opt_out_multiple_channels(self):
        settings = notification_system.settings.NotificationSettings()
        settings.opt_out("user1", notification_system.models.Channel.SMS)
        settings.opt_out("user1", notification_system.models.Channel.EMAIL)
        assert settings.is_opted_in("user1", notification_system.models.Channel.SMS) is False
        assert settings.is_opted_in("user1", notification_system.models.Channel.EMAIL) is False
        assert settings.is_opted_in("user1", notification_system.models.Channel.IOS_PUSH) is True


class TestOptIn:
    """Tests for opt-in behavior (Requirement 8.4)."""

    def test_opt_in_after_opt_out_restores_delivery(self):
        settings = notification_system.settings.NotificationSettings()
        settings.opt_out("user1", notification_system.models.Channel.EMAIL)
        assert settings.is_opted_in("user1", notification_system.models.Channel.EMAIL) is False
        settings.opt_in("user1", notification_system.models.Channel.EMAIL)
        assert settings.is_opted_in("user1", notification_system.models.Channel.EMAIL) is True

    def test_opt_in_on_already_opted_in_is_idempotent(self):
        settings = notification_system.settings.NotificationSettings()
        settings.opt_in("user1", notification_system.models.Channel.EMAIL)
        assert settings.is_opted_in("user1", notification_system.models.Channel.EMAIL) is True

    def test_opt_out_opt_in_sequence(self):
        settings = notification_system.settings.NotificationSettings()
        settings.opt_out("user1", notification_system.models.Channel.IOS_PUSH)
        settings.opt_out("user1", notification_system.models.Channel.ANDROID_PUSH)
        settings.opt_in("user1", notification_system.models.Channel.IOS_PUSH)
        assert settings.is_opted_in("user1", notification_system.models.Channel.IOS_PUSH) is True
        assert settings.is_opted_in("user1", notification_system.models.Channel.ANDROID_PUSH) is False


class TestGetAllPreferences:
    """Tests for get_all_preferences (Requirement 8.5)."""

    def test_returns_all_channels(self):
        settings = notification_system.settings.NotificationSettings()
        prefs = settings.get_all_preferences("user1")
        assert set(prefs.keys()) == set(notification_system.models.Channel)

    def test_reflects_opt_out_changes(self):
        settings = notification_system.settings.NotificationSettings()
        settings.opt_out("user1", notification_system.models.Channel.SMS)
        settings.opt_out("user1", notification_system.models.Channel.EMAIL)
        prefs = settings.get_all_preferences("user1")
        assert prefs[notification_system.models.Channel.SMS] is False
        assert prefs[notification_system.models.Channel.EMAIL] is False
        assert prefs[notification_system.models.Channel.IOS_PUSH] is True
        assert prefs[notification_system.models.Channel.ANDROID_PUSH] is True

    def test_returns_copy_not_reference(self):
        settings = notification_system.settings.NotificationSettings()
        prefs = settings.get_all_preferences("user1")
        # Mutating the returned dict should not affect internal state
        prefs[notification_system.models.Channel.EMAIL] = False
        assert settings.is_opted_in("user1", notification_system.models.Channel.EMAIL) is True


class TestBulkUpdate:
    """Tests for bulk_update (Requirement 8.6)."""

    def test_bulk_update_multiple_channels(self):
        settings = notification_system.settings.NotificationSettings()
        settings.bulk_update(
            "user1",
            {
                notification_system.models.Channel.SMS: False,
                notification_system.models.Channel.EMAIL: False,
            },
        )
        assert settings.is_opted_in("user1", notification_system.models.Channel.SMS) is False
        assert settings.is_opted_in("user1", notification_system.models.Channel.EMAIL) is False
        assert settings.is_opted_in("user1", notification_system.models.Channel.IOS_PUSH) is True

    def test_bulk_update_can_opt_in_and_out(self):
        settings = notification_system.settings.NotificationSettings()
        settings.opt_out("user1", notification_system.models.Channel.IOS_PUSH)
        settings.bulk_update(
            "user1",
            {
                notification_system.models.Channel.IOS_PUSH: True,
                notification_system.models.Channel.SMS: False,
            },
        )
        assert settings.is_opted_in("user1", notification_system.models.Channel.IOS_PUSH) is True
        assert settings.is_opted_in("user1", notification_system.models.Channel.SMS) is False

    def test_bulk_update_empty_dict_no_change(self):
        settings = notification_system.settings.NotificationSettings()
        settings.opt_out("user1", notification_system.models.Channel.EMAIL)
        settings.bulk_update("user1", {})
        assert settings.is_opted_in("user1", notification_system.models.Channel.EMAIL) is False

    def test_bulk_update_all_channels(self):
        settings = notification_system.settings.NotificationSettings()
        settings.bulk_update("user1", {ch: False for ch in notification_system.models.Channel})
        for channel in notification_system.models.Channel:
            assert settings.is_opted_in("user1", channel) is False


class TestIsOptedInSequences:
    """Tests for is_opted_in after various opt_out/opt_in sequences."""

    def test_repeated_opt_out_is_idempotent(self):
        settings = notification_system.settings.NotificationSettings()
        settings.opt_out("user1", notification_system.models.Channel.EMAIL)
        settings.opt_out("user1", notification_system.models.Channel.EMAIL)
        assert settings.is_opted_in("user1", notification_system.models.Channel.EMAIL) is False

    def test_alternating_opt_out_opt_in(self):
        settings = notification_system.settings.NotificationSettings()
        settings.opt_out("user1", notification_system.models.Channel.SMS)
        settings.opt_in("user1", notification_system.models.Channel.SMS)
        settings.opt_out("user1", notification_system.models.Channel.SMS)
        assert settings.is_opted_in("user1", notification_system.models.Channel.SMS) is False

    def test_opt_in_after_multiple_opt_outs(self):
        settings = notification_system.settings.NotificationSettings()
        settings.opt_out("user1", notification_system.models.Channel.EMAIL)
        settings.opt_out("user1", notification_system.models.Channel.EMAIL)
        settings.opt_out("user1", notification_system.models.Channel.EMAIL)
        settings.opt_in("user1", notification_system.models.Channel.EMAIL)
        assert settings.is_opted_in("user1", notification_system.models.Channel.EMAIL) is True

    def test_bulk_update_followed_by_individual_changes(self):
        settings = notification_system.settings.NotificationSettings()
        settings.bulk_update("user1", {ch: False for ch in notification_system.models.Channel})
        settings.opt_in("user1", notification_system.models.Channel.EMAIL)
        assert settings.is_opted_in("user1", notification_system.models.Channel.EMAIL) is True
        assert settings.is_opted_in("user1", notification_system.models.Channel.SMS) is False
