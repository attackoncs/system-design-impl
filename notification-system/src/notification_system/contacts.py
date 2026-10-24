"""Contact information store for the notification system.

Manages user contact information including email addresses, phone numbers, and
device tokens. Supports multiple device tokens per user for push notification
channels.
"""

import dataclasses
from typing import Optional

import notification_system.models


class ContactInfoStore:
    """In-memory store for user contact information.

    Manages email addresses, phone numbers, and device tokens. Supports
    multiple device tokens per user for push channels.
    """

    def __init__(self) -> None:
        self._contacts: dict[str, notification_system.models.ContactInfo] = {}

    def get(self, user_id: str) -> Optional[notification_system.models.ContactInfo]:
        """Retrieve contact info for a user.

        Args:
            user_id: The user identifier.

        Returns:
            The ContactInfo for the user, or None if not found.
        """
        contact = self._contacts.get(user_id)
        return self._copy(contact) if contact else None

    @staticmethod
    def _copy(contact):
        return dataclasses.replace(
            contact,
            device_tokens=list(contact.device_tokens),
            device_channels=dict(contact.device_channels),
        )

    def set(self, contact: notification_system.models.ContactInfo) -> None:
        """Store or update contact info for a user.

        Args:
            contact: The ContactInfo to store.
        """
        self._contacts[contact.user_id] = self._copy(contact)

    def remove(self, user_id: str) -> None:
        """Remove all contact info for a user.

        Args:
            user_id: The user identifier.
        """
        self._contacts.pop(user_id, None)

    def add_device_token(self, user_id: str, token: str, channel=None) -> None:
        """Add a device token for a user.

        Creates a new ContactInfo entry if the user doesn't exist.
        Ignores duplicate tokens.

        Args:
            user_id: The user identifier.
            token: The device token to add.
        """
        if channel is not None and channel not in (
            notification_system.models.Channel.IOS_PUSH,
            notification_system.models.Channel.ANDROID_PUSH,
        ):
            raise ValueError("Device channel must be ios_push or android_push")
        contact = self._contacts.get(user_id)
        if contact is None:
            contact = notification_system.models.ContactInfo(user_id=user_id)
            self._contacts[user_id] = contact
        if token not in contact.device_tokens:
            contact.device_tokens.append(token)
        if channel is not None:
            contact.device_channels[token] = channel

    def remove_device_token(self, user_id: str, token: str) -> None:
        """Remove a device token for a user.

        No-op if the user doesn't exist or the token is not registered.

        Args:
            user_id: The user identifier.
            token: The device token to remove.
        """
        contact = self._contacts.get(user_id)
        if contact and token in contact.device_tokens:
            contact.device_tokens.remove(token)
            contact.device_channels.pop(token, None)

    def resolve_endpoint(
        self, user_id: str, channel: notification_system.models.Channel
    ) -> Optional[str]:
        """Resolve the delivery endpoint for a user and channel.

        Args:
            user_id: The user identifier.
            channel: The notification channel.

        Returns:
            The endpoint string (email, phone, or first device token),
            or None if no contact info exists for that channel.
        """
        endpoints = self.resolve_endpoints(user_id, channel)
        return endpoints[0] if endpoints else None

    def resolve_endpoints(self, user_id, channel) -> list[str]:
        """Return a deduplicated snapshot of all endpoints for the channel."""
        contact = self._contacts.get(user_id)
        if contact is None:
            return []
        if channel == notification_system.models.Channel.EMAIL:
            return [contact.email] if contact.email else []
        if channel == notification_system.models.Channel.SMS:
            return [contact.phone] if contact.phone else []
        if channel in (
            notification_system.models.Channel.IOS_PUSH,
            notification_system.models.Channel.ANDROID_PUSH,
        ):
            return list(
                dict.fromkeys(
                    token
                    for token in contact.device_tokens
                    if token and contact.device_channels.get(token, channel) == channel
                )
            )
        return []
