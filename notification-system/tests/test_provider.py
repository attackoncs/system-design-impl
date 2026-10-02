"""Unit tests for Provider ABC and SimulatedProvider.

Tests channel property, deliver returns DeliveryResult, failure_rate behavior,
latency simulation, and that Provider ABC cannot be instantiated directly.
Requirements: 4.1, 4.2, 4.3, 4.6
"""

import asyncio
import time

import pytest

from notification_system.models import (
    Channel,
    DeliveryResult,
    NotificationRequest,
    NotificationTask,
)
from notification_system.provider import Provider, SimulatedProvider


def _make_task(channel: Channel = Channel.EMAIL) -> NotificationTask:
    """Helper to create a NotificationTask for testing."""
    request = NotificationRequest(
        recipient_id="user_1",
        channel=channel,
        title="Test",
        body="Hello",
    )
    return NotificationTask(
        notification_id="notif_001",
        request=request,
        channel=channel,
    )


class TestProviderABC:
    """Tests that Provider ABC cannot be instantiated directly."""

    def test_cannot_instantiate_provider_directly(self):
        with pytest.raises(TypeError):
            Provider()

    def test_subclass_must_implement_deliver(self):
        class IncompleteProvider(Provider):
            @property
            def channel(self) -> Channel:
                return Channel.EMAIL

        with pytest.raises(TypeError):
            IncompleteProvider()

    def test_subclass_must_implement_channel(self):
        class IncompleteProvider(Provider):
            async def deliver(self, task: NotificationTask) -> DeliveryResult:
                return DeliveryResult(success=True)

        with pytest.raises(TypeError):
            IncompleteProvider()


class TestSimulatedProviderChannel:
    """Tests for SimulatedProvider channel property."""

    def test_channel_returns_configured_channel(self):
        provider = SimulatedProvider(channel=Channel.EMAIL)
        assert provider.channel == Channel.EMAIL

    def test_channel_sms(self):
        provider = SimulatedProvider(channel=Channel.SMS)
        assert provider.channel == Channel.SMS

    def test_channel_ios_push(self):
        provider = SimulatedProvider(channel=Channel.IOS_PUSH)
        assert provider.channel == Channel.IOS_PUSH

    def test_channel_android_push(self):
        provider = SimulatedProvider(channel=Channel.ANDROID_PUSH)
        assert provider.channel == Channel.ANDROID_PUSH


class TestSimulatedProviderDeliver:
    """Tests for SimulatedProvider.deliver returning DeliveryResult."""

    async def test_deliver_returns_delivery_result(self):
        provider = SimulatedProvider(channel=Channel.EMAIL, latency_ms=0)
        task = _make_task(Channel.EMAIL)
        result = await provider.deliver(task)
        assert isinstance(result, DeliveryResult)

    async def test_deliver_success_has_provider_message_id(self):
        provider = SimulatedProvider(channel=Channel.EMAIL, latency_ms=0, failure_rate=0.0)
        task = _make_task(Channel.EMAIL)
        result = await provider.deliver(task)
        assert result.success is True
        assert result.provider_message_id is not None
        assert result.provider_message_id.startswith("sim_")

    async def test_deliver_success_no_error(self):
        provider = SimulatedProvider(channel=Channel.SMS, latency_ms=0, failure_rate=0.0)
        task = _make_task(Channel.SMS)
        result = await provider.deliver(task)
        assert result.success is True
        assert result.error is None


class TestSimulatedProviderFailureRate:
    """Tests for failure_rate=1.0 always fails."""

    async def test_failure_rate_one_always_fails(self):
        provider = SimulatedProvider(channel=Channel.EMAIL, latency_ms=0, failure_rate=1.0)
        task = _make_task(Channel.EMAIL)
        # Run multiple times to confirm it always fails
        for _ in range(10):
            result = await provider.deliver(task)
            assert result.success is False
            assert result.error is not None
            assert result.retryable is True

    async def test_failure_rate_zero_always_succeeds(self):
        provider = SimulatedProvider(channel=Channel.EMAIL, latency_ms=0, failure_rate=0.0)
        task = _make_task(Channel.EMAIL)
        for _ in range(10):
            result = await provider.deliver(task)
            assert result.success is True

    async def test_failure_result_has_no_provider_message_id(self):
        provider = SimulatedProvider(channel=Channel.EMAIL, latency_ms=0, failure_rate=1.0)
        task = _make_task(Channel.EMAIL)
        result = await provider.deliver(task)
        assert result.success is False
        assert result.provider_message_id is None


class TestSimulatedProviderLatency:
    """Tests for latency simulation."""

    async def test_latency_introduces_delay(self):
        latency_ms = 100.0
        provider = SimulatedProvider(channel=Channel.EMAIL, latency_ms=latency_ms, failure_rate=0.0)
        task = _make_task(Channel.EMAIL)

        start = time.perf_counter()
        await provider.deliver(task)
        elapsed_ms = (time.perf_counter() - start) * 1000

        # Allow some tolerance but ensure delay is at least close to configured latency
        assert elapsed_ms >= latency_ms * 0.8

    async def test_zero_latency_is_fast(self):
        provider = SimulatedProvider(channel=Channel.EMAIL, latency_ms=0, failure_rate=0.0)
        task = _make_task(Channel.EMAIL)

        start = time.perf_counter()
        await provider.deliver(task)
        elapsed_ms = (time.perf_counter() - start) * 1000

        # With zero latency, should complete very quickly
        assert elapsed_ms < 50
