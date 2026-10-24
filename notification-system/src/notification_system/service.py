"""NotificationService orchestrator for the notification pipeline.

Central orchestrator that coordinates the full notification pipeline:
validate → check preferences → rate limit → dedup → enqueue.

Provides the unified public API for sending notifications, managing
templates, and querying status.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
import logging
import re
import uuid
from typing import Optional

import notification_system.config
import notification_system.contacts
import notification_system.dedup
import notification_system.exceptions
import notification_system.models
import notification_system.queue
import notification_system.rate_limiter
import notification_system.settings
import notification_system.templates
import notification_system.tracker


class NotificationService:
    """Central orchestrator for the notification pipeline.

    Pipeline: validate → check preferences → rate limit → dedup → enqueue.

    Coordinates all subsystems and provides the unified public API
    for sending notifications, managing templates, and querying status.
    """

    def __init__(
        self,
        config: Optional[notification_system.config.NotificationConfig] = None,
        queue: Optional[notification_system.queue.MessageQueue] = None,
        settings: Optional[notification_system.settings.NotificationSettings] = None,
        contacts: Optional[notification_system.contacts.ContactInfoStore] = None,
        rate_limiter: Optional[
            notification_system.rate_limiter.SlidingWindowRateLimiter
        ] = None,
        deduplicator: Optional[notification_system.dedup.Deduplicator] = None,
        tracker: Optional[notification_system.tracker.EventTracker] = None,
        template_registry: Optional[
            notification_system.templates.TemplateRegistry
        ] = None,
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
        self._config = config or notification_system.config.NotificationConfig()
        self._queue = queue or notification_system.queue.MessageQueue(
            max_depth=self._config.queue_max_depth
        )
        self._settings = settings or notification_system.settings.NotificationSettings()
        self._contacts = contacts or notification_system.contacts.ContactInfoStore()
        self._rate_limiter = (
            rate_limiter
            or notification_system.rate_limiter.SlidingWindowRateLimiter(
                per_channel_limits=self._config.per_channel_rate_limits,
                global_limit=self._config.global_rate_limit,
            )
        )
        self._deduplicator = deduplicator or notification_system.dedup.Deduplicator(
            window_seconds=self._config.dedup_window_seconds
        )
        self._tracker = tracker or notification_system.tracker.EventTracker()
        self._templates = (
            template_registry or notification_system.templates.TemplateRegistry()
        )
        self._admission = [asyncio.Lock() for _ in range(64)]

    @property
    def config(self) -> notification_system.config.NotificationConfig:
        """The system configuration."""
        return self._config

    @property
    def queue(self) -> notification_system.queue.MessageQueue:
        """The message queue."""
        return self._queue

    @property
    def settings(self) -> notification_system.settings.NotificationSettings:
        """The notification settings store."""
        return self._settings

    @property
    def contacts(self) -> notification_system.contacts.ContactInfoStore:
        """The contact info store."""
        return self._contacts

    @property
    def tracker(self) -> notification_system.tracker.EventTracker:
        """The event tracker."""
        return self._tracker

    @property
    def templates(self) -> notification_system.templates.TemplateRegistry:
        """The template registry."""
        return self._templates

    def create_worker_pool(
        self, providers, notification_log=None, *, delivery_timeout=30.0
    ):
        """Wire workers to this service's queue, preferences, tracker and retry policy."""
        from notification_system.log import NotificationLog
        from notification_system.retry import RetryHandler
        from notification_system.worker import WorkerPool

        retry = RetryHandler(
            self._config.retry,
            self._queue,
            self._tracker,
            notification_log or NotificationLog(),
        )
        return WorkerPool(
            self._queue,
            providers,
            self._tracker,
            retry,
            self._config.workers_per_channel,
            settings=self._settings,
            delivery_timeout=delivery_timeout,
        )

    async def send(
        self,
        request: notification_system.models.NotificationRequest,
        *,
        scheduled_at: Optional[datetime.datetime] = None,
    ) -> str:
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
        self._validate_request(request)
        if scheduled_at is not None and (
            not isinstance(scheduled_at, datetime.datetime)
            or scheduled_at.tzinfo is None
            or scheduled_at.utcoffset() is None
        ):
            raise notification_system.exceptions.ValidationError(
                "scheduled_at must be timezone-aware"
            )
        async with self._admission[hash(request.recipient_id) % len(self._admission)]:
            return await self._accept(request, scheduled_at)

    async def _accept(self, request, scheduled_at):
        notification_id = uuid.uuid4().hex
        if request.event_id is not None:
            original = await self._queue.event_notification_id(
                request.recipient_id, request.channel, request.event_id
            )
            if original is not None:
                raise notification_system.exceptions.DuplicateNotificationError(
                    original
                )

        # 1. Validate: check contact info exists for the channel
        endpoint = self._contacts.resolve_endpoint(
            request.recipient_id, request.channel
        )
        if endpoint is None:
            raise notification_system.exceptions.ValidationError(
                f"No contact info for user '{request.recipient_id}' "
                f"on channel '{request.channel.value}'",
                field="recipient_id",
            )
        endpoints = self._contacts.resolve_endpoints(
            request.recipient_id, request.channel
        )
        if any(
            not isinstance(value, str) or not value.strip() or len(value) > 4096
            for value in endpoints
        ):
            raise notification_system.exceptions.ValidationError(
                "Invalid delivery endpoint", field="recipient_id"
            )
        if (
            request.channel == notification_system.models.Channel.EMAIL
            and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", endpoint)
        ):
            raise notification_system.exceptions.ValidationError(
                "Invalid email address", field="recipient_id"
            )
        if (
            request.channel == notification_system.models.Channel.SMS
            and not re.fullmatch(r"\+?[0-9]{7,15}", endpoint)
        ):
            raise notification_system.exceptions.ValidationError(
                "Invalid phone number", field="recipient_id"
            )

        # 2. Check preferences: reject if user opted out
        if not self._settings.is_opted_in(request.recipient_id, request.channel):
            # Record UNSUBSCRIBED event before rejecting
            self._tracker.record(
                notification_id,
                request.channel,
                notification_system.models.NotificationStatus.UNSUBSCRIBED,
            )
            raise notification_system.exceptions.ValidationError(
                f"User '{request.recipient_id}' has opted out of "
                f"'{request.channel.value}'"
            )

        # 3. Resolve content (template or direct)
        title, body = self._resolve_content(request)
        if len((title + body).encode("utf-8")) > 1024 * 1024:
            raise notification_system.exceptions.ValidationError(
                "Resolved content exceeds 1 MiB"
            )
        if len(endpoints) > 100:
            raise notification_system.exceptions.ValidationError(
                "At most 100 device endpoints are supported"
            )

        # 4. Rate limit check and record
        self._rate_limiter.check(request.recipient_id, request.channel)

        # 5. Deduplication check and record
        content = json.dumps([title, body], separators=(",", ":"))
        dedup_key = self._deduplicator.compute_key(
            request.recipient_id, request.channel, content
        )
        original = (
            self._deduplicator.check(dedup_key) if request.event_id is None else None
        )
        if original is not None:
            raise notification_system.exceptions.DuplicateNotificationError(original)

        # 6. Enqueue to the appropriate channel queue
        task = notification_system.models.NotificationTask(
            notification_id=notification_id,
            request=dataclasses.replace(
                request,
                title=title,
                body=body,
                template_params=dict(request.template_params),
                metadata=dict(request.metadata),
            ),
            channel=request.channel,
            endpoints=tuple(endpoints),
            scheduled_at=scheduled_at,
        )
        self._tracker.record(
            notification_id,
            request.channel,
            notification_system.models.NotificationStatus.CREATED,
        )
        # Set the local state before making the task visible to workers.
        self._tracker.record(
            notification_id,
            request.channel,
            notification_system.models.NotificationStatus.QUEUED,
        )
        try:
            await self._queue.enqueue(task)
        except Exception:
            self._tracker.record(
                notification_id,
                request.channel,
                notification_system.models.NotificationStatus.FAILED,
            )
            raise
        # Rejections must not consume quota or poison content deduplication.
        self._rate_limiter.record(request.recipient_id, request.channel)
        if request.event_id is None:
            self._deduplicator.record(dedup_key, notification_id)
        return notification_id

    @staticmethod
    def _validate_request(request):
        if not isinstance(request, notification_system.models.NotificationRequest):
            raise notification_system.exceptions.ValidationError(
                "Expected NotificationRequest"
            )
        if not isinstance(request.channel, notification_system.models.Channel):
            raise notification_system.exceptions.ValidationError(
                "Invalid channel", field="channel"
            )
        if (
            not isinstance(request.recipient_id, str)
            or not request.recipient_id.strip()
            or len(request.recipient_id) > 256
        ):
            raise notification_system.exceptions.ValidationError(
                "Invalid recipient_id", field="recipient_id"
            )
        for name in ("title", "body"):
            if not isinstance(getattr(request, name), str):
                raise notification_system.exceptions.ValidationError(
                    f"{name} must be a string", field=name
                )
        if request.template_id is not None and not isinstance(request.template_id, str):
            raise notification_system.exceptions.ValidationError(
                "template_id must be a string", field="template_id"
            )
        if request.event_id is not None and (
            not isinstance(request.event_id, str)
            or not request.event_id.strip()
            or len(request.event_id) > 256
        ):
            raise notification_system.exceptions.ValidationError(
                "event_id must be a nonempty string of at most 256 characters",
                field="event_id",
            )
        for name in ("template_params", "metadata"):
            value = getattr(request, name)
            if not isinstance(value, dict) or any(
                not isinstance(k, str) or not isinstance(v, str)
                for k, v in value.items()
            ):
                raise notification_system.exceptions.ValidationError(
                    f"{name} must map strings to strings", field=name
                )

    async def send_batch(
        self, requests: list[notification_system.models.NotificationRequest]
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
            # Isolate this operation; record failures before continuing.
            except Exception:  # pylint: disable=broad-exception-caught
                logging.getLogger(__name__).exception(
                    "Failure isolated in %s", "send_batch"
                )
                results.append("")
        return results

    def _resolve_content(
        self, request: notification_system.models.NotificationRequest
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
                raise notification_system.exceptions.ValidationError(
                    f"Template '{request.template_id}' not found",
                    field="template_id",
                )
            return template.render(request.template_params)
        return request.title, request.body
