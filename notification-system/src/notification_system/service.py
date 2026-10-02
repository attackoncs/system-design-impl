"""NotificationService orchestrator for the notification pipeline.

Central orchestrator that coordinates the full notification pipeline:
validate → check preferences → rate limit → dedup → enqueue.

Provides the unified public API for sending notifications, managing
templates, and querying status.
"""

from __future__ import annotations

import uuid
from typing import Optional

from notification_system.config import NotificationConfig
from notification_system.contacts import ContactInfoStore
from notification_system.dedup import Deduplicator
from notification_system.exceptions import ValidationError
from notification_system.models import (
    Channel,
    NotificationRequest,
    NotificationStatus,
    NotificationTask,
)
from notification_system.queue import MessageQueue
from notification_system.rate_limiter import SlidingWindowRateLimiter
from notification_system.settings import NotificationSettings
from notification_system.templates import TemplateRegistry
from notification_system.tracker import EventTracker


class NotificationService:
    """Central orchestrator for the notification pipeline.

    Pipeline: validate → check preferences → rate limit → dedup → enqueue.

    Coordinates all subsystems and provides the unified public API
    for sending notifications, managing templates, and querying status.
    """

    def __init__(
        self,
        config: Optional[NotificationConfig] = None,
        queue: Optional[MessageQueue] = None,
        settings: Optional[NotificationSettings] = None,
        contacts: Optional[ContactInfoStore] = None,
        rate_limiter: Optional[SlidingWindowRateLimiter] = None,
        deduplicator: Optional[Deduplicator] = None,
        tracker: Optional[EventTracker] = None,
        template_registry: Optional[TemplateRegistry] = None,
    ) -> None:
        """Initialize the NotificationService with all pipeline components.

        Args:
            config: System configuration. Defaults to NotificationConfig().
            queue: Message queue for per-channel task buffering.
            settings: User notification preferences store.
            contacts: User contact information store.
            rate_limiter: Sliding window rate limiter.
            deduplicator: Content-hash deduplicator.
            tracker: Lifecycle event tracker.
            template_registry: Template registry for content resolution.
        """
        self._config = config or NotificationConfig()
        self._queue = queue or MessageQueue(max_depth=self._config.queue_max_depth)
        self._settings = settings or NotificationSettings()
        self._contacts = contacts or ContactInfoStore()
        self._rate_limiter = rate_limiter or SlidingWindowRateLimiter(
            per_channel_limits=self._config.per_channel_rate_limits,
            global_limit=self._config.global_rate_limit,
        )
        self._deduplicator = deduplicator or Deduplicator(
            window_seconds=self._config.dedup_window_seconds
        )
        self._tracker = tracker or EventTracker()
        self._templates = template_registry or TemplateRegistry()

    @property
    def config(self) -> NotificationConfig:
        """The system configuration."""
        return self._config

    @property
    def queue(self) -> MessageQueue:
        """The message queue."""
        return self._queue

    @property
    def settings(self) -> NotificationSettings:
        """The notification settings store."""
        return self._settings

    @property
    def contacts(self) -> ContactInfoStore:
        """The contact info store."""
        return self._contacts

    @property
    def tracker(self) -> EventTracker:
        """The event tracker."""
        return self._tracker

    @property
    def templates(self) -> TemplateRegistry:
        """The template registry."""
        return self._templates

    async def send(self, request: NotificationRequest) -> str:
        """Send a single notification through the pipeline.

        Pipeline steps:
        1. Validate request (contact info exists)
        2. Check user preferences (opt-in/out)
        3. Resolve content (template or direct)
        4. Rate limit check
        5. Deduplication check
        6. Enqueue to channel queue

        Args:
            request: The notification request.

        Returns:
            Unique notification identifier (uuid4 hex).

        Raises:
            ValidationError: If request is invalid (missing contact info,
                opted out, or template not found).
            RateLimitExceededError: If rate limit exceeded.
            DuplicateNotificationError: If duplicate detected.
            QueueFullError: If the channel queue is full.
        """
        notification_id = uuid.uuid4().hex

        # 1. Validate: check contact info exists for the channel
        endpoint = self._contacts.resolve_endpoint(
            request.recipient_id, request.channel
        )
        if endpoint is None:
            raise ValidationError(
                f"No contact info for user '{request.recipient_id}' "
                f"on channel '{request.channel.value}'",
                field="recipient_id",
            )

        # 2. Check preferences: reject if user opted out
        if not self._settings.is_opted_in(request.recipient_id, request.channel):
            # Record UNSUBSCRIBED event before rejecting
            self._tracker.record(
                notification_id,
                request.channel,
                NotificationStatus.UNSUBSCRIBED,
            )
            raise ValidationError(
                f"User '{request.recipient_id}' has opted out of "
                f"'{request.channel.value}'"
            )

        # Record CREATED event
        self._tracker.record(
            notification_id, request.channel, NotificationStatus.CREATED
        )

        # 3. Resolve content (template or direct)
        title, body = self._resolve_content(request)

        # 4. Rate limit check and record
        self._rate_limiter.check(request.recipient_id, request.channel)
        self._rate_limiter.record(request.recipient_id, request.channel)

        # 5. Deduplication check and record
        content = f"{title}:{body}"
        self._deduplicator.check_and_record(
            request.recipient_id, request.channel, content, notification_id
        )

        # 6. Enqueue to the appropriate channel queue
        task = NotificationTask(
            notification_id=notification_id,
            request=request,
            channel=request.channel,
        )
        await self._queue.enqueue(task)

        # Record QUEUED event
        self._tracker.record(
            notification_id, request.channel, NotificationStatus.QUEUED
        )

        return notification_id

    async def send_batch(
        self, requests: list[NotificationRequest]
    ) -> list[str]:
        """Send notifications to multiple recipients in a single batch.

        Iterates over each request and calls send(). Failed requests
        return an empty string in their position.

        Args:
            requests: List of notification requests.

        Returns:
            List of notification identifiers (one per request).
            Failed requests have an empty string in their position.
        """
        results: list[str] = []
        for request in requests:
            try:
                nid = await self.send(request)
                results.append(nid)
            except Exception:
                results.append("")
        return results

    def _resolve_content(
        self, request: NotificationRequest
    ) -> tuple[str, str]:
        """Resolve notification content from template or direct fields.

        If template_id is set on the request, looks up the template in
        the registry and renders it with the provided params. Otherwise,
        uses the direct title and body fields from the request.

        Args:
            request: The notification request.

        Returns:
            Tuple of (title, body) strings.

        Raises:
            ValidationError: If template_id is specified but not found.
            TemplateRenderError: If template rendering fails (missing keys).
        """
        if request.template_id:
            template = self._templates.get(request.template_id)
            if template is None:
                raise ValidationError(
                    f"Template '{request.template_id}' not found",
                    field="template_id",
                )
            return template.render(request.template_params)
        return request.title, request.body
