"""Custom exception hierarchy for the notification system.

All exceptions inherit from NotificationError, providing a consistent
base for error handling across the notification pipeline.
"""

from __future__ import annotations


class NotificationError(Exception):
    """Base exception for all notification system errors."""

    pass


class ValidationError(NotificationError):
    """Raised when a notification request is invalid."""

    def __init__(self, message: str, field: str = "") -> None:
        self.field = field
        super().__init__(message)


class QueueFullError(NotificationError):
    """Raised when a channel queue has reached maximum capacity."""

    def __init__(self, channel: str, max_depth: int) -> None:
        self.channel = channel
        self.max_depth = max_depth
        super().__init__(
            f"Queue for channel '{channel}' is full (max_depth={max_depth})"
        )


class RateLimitExceededError(NotificationError):
    """Raised when a notification exceeds the rate limit for a recipient."""

    def __init__(self, user_id: str, channel: str, limit: int, window: float) -> None:
        self.user_id = user_id
        self.channel = channel
        self.limit = limit
        self.window = window
        super().__init__(
            f"Rate limit exceeded for user '{user_id}' on channel '{channel}': "
            f"{limit} per {window}s"
        )


class DuplicateNotificationError(NotificationError):
    """Raised when a notification is suppressed as a duplicate."""

    def __init__(self, original_id: str) -> None:
        self.original_id = original_id
        super().__init__(
            f"Duplicate notification suppressed (original_id='{original_id}')"
        )


class TemplateRenderError(NotificationError):
    """Raised when a notification template cannot be rendered."""

    def __init__(self, template_id: str, missing_keys: list[str]) -> None:
        self.template_id = template_id
        self.missing_keys = missing_keys
        super().__init__(
            f"Template '{template_id}' missing required keys: {missing_keys}"
        )


class DeliveryError(NotificationError):
    """Raised when a provider fails to deliver a notification."""

    def __init__(
        self, notification_id: str, channel: str, reason: str, retryable: bool = True
    ) -> None:
        self.notification_id = notification_id
        self.channel = channel
        self.reason = reason
        self.retryable = retryable
        super().__init__(
            f"Delivery failed for '{notification_id}' on '{channel}': {reason}"
        )
