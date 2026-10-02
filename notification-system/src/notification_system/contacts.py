"""Contact information store for the notification system.

Manages user contact information including email addresses, phone numbers,
and device tokens. Supports multiple device tokens per user for push
notification channels.
"""

from typing import Optional

from notification_system.models import Channel, ContactInfo


class ContactInfoStore:
    """In-memory store for user contact information.

    Manages email addresses, phone numbers, and device tokens.
    Supports multiple device tokens per user for push channels.
    """

    def __init__(self) -> None:
        self._contacts: dict[str, ContactInfo] = {}

    def get(self, user_id: str) -> Optional[ContactInfo]:
        """Retrieve contact info for a user.

        Args:
            user_id: The user identifier.

        Returns:
            The ContactInfo for the user, or None if not found.
        """
        return self._contacts.get(user_id)

    def set(self, contact: ContactInfo) -> None:
        """Store or update contact info for a user.

        Args:
            contact: The ContactInfo to store.
        """
        self._contacts[contact.user_id] = contact

    def remove(self, user_id: str) -> None:
        """Remove all contact info for a user.

        Args:
            user_id: The user identifier.
        """
        self._contacts.pop(user_id, None)

    def add_device_token(self, user_id: str, token: str) -> None:
        """Add a device token for a user.

        Creates a new ContactInfo entry if the user doesn't exist.
        Ignores duplicate tokens.

        Args:
            user_id: The user identifier.
            token: The device token to add.
        """
        contact = self._contacts.get(user_id)
        if contact is None:
            contact = ContactInfo(user_id=user_id)
            self._contacts[user_id] = contact
        if token not in contact.device_tokens:
            contact.device_tokens.append(token)

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

    def resolve_endpoint(self, user_id: str, channel: Channel) -> Optional[str]:
        """Resolve the delivery endpoint for a user and channel.

        Args:
            user_id: The user identifier.
            channel: The notification channel.

        Returns:
            The endpoint string (email, phone, or first device token),
            or None if no contact info exists for that channel.
        """
        contact = self._contacts.get(user_id)
        if contact is None:
            return None

        if channel == Channel.EMAIL:
            return contact.email
        elif channel == Channel.SMS:
            return contact.phone
        elif channel in (Channel.IOS_PUSH, Channel.ANDROID_PUSH):
            return contact.device_tokens[0] if contact.device_tokens else None
        return None
