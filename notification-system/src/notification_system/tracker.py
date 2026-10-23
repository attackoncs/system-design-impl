"""Event tracker with state machine validation and analytics.

Records lifecycle events for each notification, validates state transitions,
and provides aggregate analytics and per-channel statistics.
"""

from __future__ import annotations

import collections
import datetime
from typing import Optional

import notification_system.models


class EventTracker:
    """Lifecycle event tracker with state machine validation and analytics.

    Records state transitions for each notification. Validates that transitions
    follow the defined state machine. Provides aggregate analytics and per-
    channel statistics.
    """

    def __init__(self, clock: Optional[callable] = None) -> None:
        self._clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        # notification_id -> list of events (ordered by time)
        self._events: dict[str, list[notification_system.models.NotificationEvent]] = (
            collections.defaultdict(list)
        )

    def record(
        self,
        notification_id: str,
        channel: notification_system.models.Channel,
        status: notification_system.models.NotificationStatus,
        details: Optional[str] = None,
    ) -> None:
        """Record a lifecycle event for a notification.

        Validates that the transition from the current status to the new
        status is allowed by the state machine. If the notification has no
        prior events, any status is accepted as the initial state.

        Args:
            notification_id: The notification identifier.
            channel: The notification channel.
            status: The new status.
            details: Optional details about the transition.

        Raises:
            ValueError: If the state transition is invalid.
        """
        existing_events = self._events[notification_id]

        # Validate state transition if there are prior events
        if existing_events:
            current_status = existing_events[-1].status
            allowed = notification_system.models.VALID_TRANSITIONS.get(
                current_status, set()
            )
            if status not in allowed:
                raise ValueError(
                    "Invalid state transition for notification"
                    f" '{notification_id}': {current_status.value} ->"
                    f" {status.value}"
                )

        event = notification_system.models.NotificationEvent(
            notification_id=notification_id,
            channel=channel,
            status=status,
            timestamp=self._clock(),
            details=details,
        )
        self._events[notification_id].append(event)

    def get_history(
        self, notification_id: str
    ) -> list[notification_system.models.NotificationEvent]:
        """Get the full event history for a notification.

        Args:
            notification_id: The notification identifier.

        Returns:
            List of NotificationEvent in chronological order.
        """
        return list(self._events.get(notification_id, []))

    def get_current_status(
        self, notification_id: str
    ) -> Optional[notification_system.models.NotificationStatus]:
        """Get the current (latest) status for a notification."""
        events = self._events.get(notification_id)
        if not events:
            return None
        return events[-1].status

    def get_aggregate_counts(
        self,
        start_time: Optional[datetime.datetime] = None,
        end_time: Optional[datetime.datetime] = None,
    ) -> dict[str, int]:
        """Get aggregate counts of notifications by final status.

        Args:
            start_time: Optional start of time range filter.
            end_time: Optional end of time range filter.

        Returns:
            Dict with keys: total_sent, total_delivered, total_failed,
            total_clicked.
        """
        counts = {
            "total_sent": 0,
            "total_delivered": 0,
            "total_failed": 0,
            "total_clicked": 0,
        }
        for events in self._events.values():
            if not events:
                continue
            last_event = events[-1]
            if start_time and last_event.timestamp < start_time:
                continue
            if end_time and last_event.timestamp > end_time:
                continue
            if last_event.status == notification_system.models.NotificationStatus.SENT:
                counts["total_sent"] += 1
            elif (
                last_event.status
                == notification_system.models.NotificationStatus.DELIVERED
            ):
                counts["total_delivered"] += 1
            elif (
                last_event.status
                == notification_system.models.NotificationStatus.FAILED
            ):
                counts["total_failed"] += 1
            elif (
                last_event.status
                == notification_system.models.NotificationStatus.CLICKED
            ):
                counts["total_clicked"] += 1
        return counts

    def get_channel_stats(
        self, channel: notification_system.models.Channel
    ) -> dict[str, float]:
        """Get delivery statistics for a specific channel.

        Returns:
            Dict with success_rate, failure_rate, and total count.
        """
        total = 0
        success = 0
        failed = 0
        for events in self._events.values():
            if not events:
                continue
            # Check if this notification is for the given channel
            if events[0].channel != channel:
                continue
            total += 1
            last_status = events[-1].status
            if last_status in (
                notification_system.models.NotificationStatus.SENT,
                notification_system.models.NotificationStatus.DELIVERED,
                notification_system.models.NotificationStatus.CLICKED,
            ):
                success += 1
            elif last_status == notification_system.models.NotificationStatus.FAILED:
                failed += 1

        return {
            "total": total,
            "success_rate": success / total if total > 0 else 0.0,
            "failure_rate": failed / total if total > 0 else 0.0,
        }
