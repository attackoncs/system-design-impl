"""Notification settings for per-user, per-channel opt-in/opt-out preferences.

Provides the NotificationSettings class that manages user notification
preferences with default opt-in for all channels and support for
individual and bulk preference updates.
"""

from __future__ import annotations

from notification_system.models import Channel


class NotificationSettings:
    """Per-user, per-channel notification preference store.

    Defaults to opted-in for all channels when a user is first seen.
    Supports opt-in, opt-out, bulk updates, and preference queries.
    """

    def __init__(self) -> None:
        # user_id -> {channel -> opted_in}
        self._preferences: dict[str, dict[Channel, bool]] = {}

    def _ensure_user(self, user_id: str) -> None:
        """Initialize default preferences (all opted-in) for a new user."""
        if user_id not in self._preferences:
            self._preferences[user_id] = {channel: True for channel in Channel}

    def is_opted_in(self, user_id: str, channel: Channel) -> bool:
        """Check if a user is opted in for a channel.

        Args:
            user_id: The user identifier.
            channel: The notification channel.

        Returns:
            True if opted in (default), False if opted out.
        """
        self._ensure_user(user_id)
        return self._preferences[user_id][channel]

    def opt_out(self, user_id: str, channel: Channel) -> None:
        """Opt a user out of a notification channel.

        Args:
            user_id: The user identifier.
            channel: The channel to opt out of.
        """
        self._ensure_user(user_id)
        self._preferences[user_id][channel] = False

    def opt_in(self, user_id: str, channel: Channel) -> None:
        """Opt a user back in to a notification channel.

        Args:
            user_id: The user identifier.
            channel: The channel to opt in to.
        """
        self._ensure_user(user_id)
        self._preferences[user_id][channel] = True

    def get_all_preferences(self, user_id: str) -> dict[Channel, bool]:
        """Get all channel preferences for a user.

        Args:
            user_id: The user identifier.

        Returns:
            Dict mapping each Channel to its opt-in status.
        """
        self._ensure_user(user_id)
        return dict(self._preferences[user_id])

    def bulk_update(self, user_id: str, preferences: dict[Channel, bool]) -> None:
        """Update multiple channel preferences in a single operation.

        Args:
            user_id: The user identifier.
            preferences: Dict mapping channels to their new opt-in status.
        """
        self._ensure_user(user_id)
        for channel, opted_in in preferences.items():
            self._preferences[user_id][channel] = opted_in
