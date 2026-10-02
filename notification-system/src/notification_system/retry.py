"""Retry handler with exponential backoff and jitter for failed deliveries.

Implements configurable retry logic that distinguishes between retryable
and non-retryable errors, computes exponential backoff delays with jitter,
and re-enqueues failed tasks or marks them as permanently failed.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from notification_system.config import RetryConfig
from notification_system.exceptions import DeliveryError
from notification_system.models import NotificationStatus, NotificationTask
from notification_system.queue import MessageQueue
from notification_system.tracker import EventTracker
from notification_system.log import NotificationLog

logger = logging.getLogger(__name__)


class RetryHandler:
    """Handles failed notification deliveries with exponential backoff and jitter.

    Computes delay as: min(base_delay * 2^attempt + jitter, max_delay)
    where jitter is a random value in [0, jitter_factor * base_delay * 2^attempt].

    Non-retryable errors (invalid token, permanent rejection) skip retry
    and immediately mark the notification as FAILED.
    """

    def __init__(
        self,
        config: RetryConfig,
        queue: MessageQueue,
        tracker: EventTracker,
        notification_log: NotificationLog,
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
        exponential = self._config.base_delay * (2 ** attempt)
        jitter = random.uniform(0, self._config.jitter_factor * exponential)
        return min(exponential + jitter, self._config.max_delay)

    async def handle_failure(self, task: NotificationTask, error: DeliveryError) -> None:
        """Handle a failed delivery attempt.

        If the error is retryable and max retries not exceeded, re-enqueue
        with exponential backoff delay. Otherwise, mark as permanently failed.

        Args:
            task: The failed notification task.
            error: The delivery error with retryable flag.
        """
        # Log the retry attempt
        self._log.record_attempt(
            notification_id=task.notification_id,
            recipient_id=task.request.recipient_id,
            channel=task.channel,
            content_summary=task.request.body[:100],
            status=NotificationStatus.FAILED,
            attempt=task.attempt + 1,
            error_details=error.reason,
        )

        # Non-retryable errors: immediate permanent failure
        if not error.retryable:
            self._tracker.record(
                task.notification_id, task.channel, NotificationStatus.FAILED
            )
            logger.info(
                "Non-retryable error for %s: %s", task.notification_id, error.reason
            )
            return

        # Check max retries
        if task.attempt >= self._config.max_retries:
            self._tracker.record(
                task.notification_id, task.channel, NotificationStatus.FAILED
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
        await asyncio.sleep(delay)

        # Re-enqueue with incremented attempt counter
        retry_task = NotificationTask(
            notification_id=task.notification_id,
            request=task.request,
            channel=task.channel,
            attempt=task.attempt + 1,
            created_at=task.created_at,
            scheduled_at=datetime.now(timezone.utc),
        )
        await self._queue.enqueue(retry_task)
