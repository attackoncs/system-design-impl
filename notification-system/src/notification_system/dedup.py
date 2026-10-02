"""Content-hash based deduplication with configurable TTL.

Detects and suppresses duplicate notification requests based on a SHA-256
content hash of (recipient_id, channel, content). Maintains an in-memory
dictionary of dedup keys with expiration timestamps and automatically
expires old entries to prevent unbounded memory growth.
"""

from __future__ import annotations

import hashlib
import time
from typing import Optional

from notification_system.exceptions import DuplicateNotificationError
from notification_system.models import Channel


class Deduplicator:
    """Content-hash based deduplication with configurable TTL.

    Computes a deduplication key from (recipient_id, channel, content_hash).
    Maintains an in-memory dict of dedup keys with expiration timestamps.
    Automatically expires old entries on access.
    """

    def __init__(self, window_seconds: float = 300.0, clock: callable = None) -> None:
        self._window_seconds = window_seconds
        self._clock = clock or time.monotonic
        # dedup_key -> (notification_id, expiry_timestamp)
        self._entries: dict[str, tuple[str, float]] = {}

    def compute_key(self, recipient_id: str, channel: Channel, content: str) -> str:
        """Compute a deduplication key from recipient, channel, and content.

        Args:
            recipient_id: The recipient user ID.
            channel: The notification channel.
            content: The notification content (title + body).

        Returns:
            SHA-256 hex digest of the combined key components.
        """
        raw = f"{recipient_id}:{channel.value}:{content}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def check(self, dedup_key: str) -> Optional[str]:
        """Check if a dedup key exists and is not expired.

        Args:
            dedup_key: The deduplication key to check.

        Returns:
            The original notification_id if duplicate, None otherwise.
        """
        self._expire_old_entries()
        entry = self._entries.get(dedup_key)
        if entry is not None:
            notification_id, expiry = entry
            if self._clock() < expiry:
                return notification_id
            else:
                del self._entries[dedup_key]
        return None

    def record(self, dedup_key: str, notification_id: str) -> None:
        """Record a dedup key with the associated notification ID.

        Args:
            dedup_key: The deduplication key.
            notification_id: The notification ID to associate.
        """
        expiry = self._clock() + self._window_seconds
        self._entries[dedup_key] = (notification_id, expiry)

    def check_and_record(
        self, recipient_id: str, channel: Channel, content: str, notification_id: str
    ) -> None:
        """Check for duplicate and record if new.

        Convenience method that combines compute_key, check, and record
        into a single atomic operation.

        Args:
            recipient_id: The recipient user ID.
            channel: The notification channel.
            content: The notification content.
            notification_id: The new notification's ID.

        Raises:
            DuplicateNotificationError: If a duplicate is detected.
        """
        key = self.compute_key(recipient_id, channel, content)
        original_id = self.check(key)
        if original_id is not None:
            raise DuplicateNotificationError(original_id)
        self.record(key, notification_id)

    def _expire_old_entries(self) -> None:
        """Remove all expired entries."""
        now = self._clock()
        expired_keys = [k for k, (_, expiry) in self._entries.items() if now >= expiry]
        for k in expired_keys:
            del self._entries[k]

    @property
    def entry_count(self) -> int:
        """Number of active (non-expired) dedup entries."""
        self._expire_old_entries()
        return len(self._entries)
