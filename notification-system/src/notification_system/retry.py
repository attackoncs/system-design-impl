"""Retry handler with exponential backoff and jitter for failed deliveries.

Implements configurable retry logic that distinguishes between retryable and
non-retryable errors, computes exponential backoff delays with jitter, and re-
enqueues failed tasks or marks them as permanently failed.
"""

from __future__ import annotations

import asyncio
import datetime
import dataclasses
import logging
import random

import notification_system.config
import notification_system.exceptions
import notification_system.log
import notification_system.models
import notification_system.queue
import notification_system.tracker

logger = logging.getLogger(__name__)


class RetryHandler:
    """Handles failed notification deliveries with exponential backoff and
    jitter.

    Computes delay as: min(base_delay * 2^attempt + jitter, max_delay)
    where jitter is a random value in [0, jitter_factor * base_delay *
    2^attempt].

    Non-retryable errors (invalid token, permanent rejection) skip retry
    and immediately mark the notification as FAILED.
    """

    def __init__(
        self,
        config: notification_system.config.RetryConfig,
        queue: notification_system.queue.MessageQueue,
        tracker: notification_system.tracker.EventTracker,
        notification_log: notification_system.log.NotificationLog,
    ) -> None:
        self._config = config
        self._queue = queue
        self._tracker = tracker
        self._log = notification_log

    def compute_delay(self, attempt: int) -> float:
        """Compute the backoff delay for a given attempt number.

        Formula: min(base_delay * 2^attempt + jitter, max_delay)

        Args:
            attempt: The retry attempt number (0-indexed).

        Returns:
            Delay in seconds.
        """
        # Clamp the exponent before constructing huge integers or overflowing.
        if attempt < 0:
            raise ValueError("attempt must be non-negative")
        exponential = min(
            self._config.base_delay * (2 ** min(attempt, 1023)), self._config.max_delay
        )
        jitter = random.uniform(0, self._config.jitter_factor * exponential)
        return min(exponential + jitter, self._config.max_delay)

    async def handle_failure(
        self,
        task: notification_system.models.NotificationTask,
        error: notification_system.exceptions.DeliveryError,
        *,
        defer: bool = False,
    ) -> None:
        """Handle a failed delivery attempt.

        If the error is retryable and max retries not exceeded, re-enqueue
        with exponential backoff delay. Otherwise, mark as permanently failed.

        Args:
            task: The failed notification task.
            error: The delivery error with retryable flag.
        """
        # Log the retry attempt
        await asyncio.to_thread(
            self._log.record_attempt,
            notification_id=task.notification_id,
            recipient_id=task.request.recipient_id,
            channel=task.channel,
            content_summary=task.request.body[:100],
            status=notification_system.models.NotificationStatus.FAILED,
            attempt=task.attempt + 1,
            error_details=error.reason,
        )

        # Non-retryable errors: immediate permanent failure
        if not error.retryable:
            await self._queue.ack(
                task, notification_system.models.NotificationStatus.FAILED, error.reason
            )
            self._tracker.record(
                task.notification_id,
                task.channel,
                notification_system.models.NotificationStatus.FAILED,
            )
            logger.info(
                "Non-retryable error for %s: %s",
                task.notification_id,
                error.reason,
            )
            return

        # Check max retries
        if task.attempt >= self._config.max_retries:
            await self._queue.ack(
                task, notification_system.models.NotificationStatus.FAILED, error.reason
            )
            self._tracker.record(
                task.notification_id,
                task.channel,
                notification_system.models.NotificationStatus.FAILED,
            )
            logger.info(
                "Max retries (%d) exceeded for %s",
                self._config.max_retries,
                task.notification_id,
            )
            return

        # Compute delay and schedule retry
        delay = self.compute_delay(task.attempt)
        logger.debug(
            "Retrying %s in %.2fs (attempt %d)",
            task.notification_id,
            delay,
            task.attempt + 1,
        )
        if not defer:
            await asyncio.sleep(delay)

        # Re-enqueue with incremented attempt counter
        retry_task = dataclasses.replace(
            task,
            attempt=task.attempt + 1,
            scheduled_at=datetime.datetime.now(datetime.timezone.utc),
        )
        await self._queue.reschedule(retry_task, delay if defer else 0.0)
        current = self._tracker.get_current_status(task.notification_id)
        if current == notification_system.models.NotificationStatus.SENDING:
            self._tracker.record(
                task.notification_id,
                task.channel,
                notification_system.models.NotificationStatus.QUEUED,
            )

    async def record_success(self, task):
        """Record successful provider acceptance as well as failed attempts."""
        await asyncio.to_thread(
            self._log.record_attempt,
            task.notification_id,
            task.request.recipient_id,
            task.channel,
            task.request.body[:100],
            notification_system.models.NotificationStatus.SENT,
            task.attempt + 1,
        )
