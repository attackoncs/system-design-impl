"""Provider interface and simulated implementation for notification delivery.

Defines the abstract Provider interface that all notification delivery
providers must implement, and a SimulatedProvider for testing with configurable
latency and failure rate.
"""

from __future__ import annotations

import abc
import asyncio
import random
import uuid

import notification_system.models


class Provider(abc.ABC):
    """Abstract interface for notification delivery providers.

    Each provider handles delivery for a specific channel type. Implementations
    must be async-compatible.
    """

    @abc.abstractmethod
    async def deliver(
        self, task: notification_system.models.NotificationTask
    ) -> notification_system.models.DeliveryResult:
        """Deliver a notification through this provider.

        Args:
            task: The notification task containing recipient and content.

        Returns:
            DeliveryResult with success status and provider message ID.
        """

    @property
    @abc.abstractmethod
    def channel(self) -> notification_system.models.Channel:
        """The channel this provider handles."""


class SimulatedProvider(Provider):
    """Simulated provider for testing with configurable behavior.

    Simulates delivery with configurable latency and failure rate. Useful for
    testing the full pipeline without real third-party services.
    """

    def __init__(
        self,
        channel: notification_system.models.Channel,
        latency_ms: float = 50.0,
        failure_rate: float = 0.0,
    ) -> None:
        """Initialize the simulated provider.

        Args:
            channel: The notification channel this provider handles.
            latency_ms: Simulated network latency in milliseconds.
            failure_rate: Probability of simulated failure (0.0 to 1.0).
        """
        self._channel = channel
        self._latency_ms = latency_ms
        self._failure_rate = failure_rate

    @property
    def channel(self) -> notification_system.models.Channel:
        """The channel this provider handles."""
        return self._channel

    async def deliver(
        self, task: notification_system.models.NotificationTask
    ) -> notification_system.models.DeliveryResult:
        """Simulate delivery with configurable latency and failure.

        Args:
            task: The notification task to deliver.

        Returns:
            DeliveryResult indicating success or simulated failure.
        """
        # Simulate network latency
        await asyncio.sleep(self._latency_ms / 1000.0)

        # Simulate random failures
        if random.random() < self._failure_rate:
            return notification_system.models.DeliveryResult(
                success=False,
                error="Simulated delivery failure",
                retryable=True,
            )

        return notification_system.models.DeliveryResult(
            success=True,
            provider_message_id=f"sim_{uuid.uuid4().hex[:12]}",
        )
