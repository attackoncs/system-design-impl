"""Unit tests for the NotificationService orchestrator.

Tests the full pipeline: validation, preferences check, rate limiting,
deduplication, successful enqueue, batch send, and template resolution.
"""

from __future__ import annotations

import asyncio

import pytest

from notification_system.config import NotificationConfig, RateLimitConfig
from notification_system.contacts import ContactInfoStore
from notification_system.exceptions import (
    DuplicateNotificationError,
    RateLimitExceededError,
    ValidationError,
)
from notification_system.models import (
    Channel,
    ContactInfo,
    NotificationRequest,
)
from notification_system.service import NotificationService
from notification_system.settings import NotificationSettings
from notification_system.templates import NotificationTemplate, TemplateRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def contacts() -> ContactInfoStore:
    """ContactInfoStore pre-populated with test users."""
    store = ContactInfoStore()
    store.set(
        ContactInfo(
            user_id="user1",
            email="user1@example.com",
            phone="+15551234567",
            device_tokens=["token_ios_1"],
        )
    )
    store.set(
        ContactInfo(
            user_id="user2",
            email="user2@example.com",
            phone="+15559876543",
            device_tokens=["token_android_2"],
        )
    )
    store.set(
        ContactInfo(
            user_id="user3",
            email="user3@example.com",
            phone=None,
            device_tokens=[],
        )
    )
    return store


@pytest.fixture
def settings() -> NotificationSettings:
    """NotificationSettings with default opt-in for all users."""
    return NotificationSettings()


@pytest.fixture
def template_registry() -> TemplateRegistry:
    """TemplateRegistry with a test template registered."""
    registry = TemplateRegistry()
    registry.register(
        NotificationTemplate(
            template_id="welcome",
            title="Welcome, {name}!",
            body="Hello {name}, thanks for joining {app}.",
        )
    )
    return registry


@pytest.fixture
def config() -> NotificationConfig:
    """NotificationConfig with low rate limits for testing."""
    return NotificationConfig(
        per_channel_rate_limits={
            Channel.IOS_PUSH: RateLimitConfig(max_count=3, window_seconds=60.0),
            Channel.ANDROID_PUSH: RateLimitConfig(max_count=3, window_seconds=60.0),
            Channel.SMS: RateLimitConfig(max_count=2, window_seconds=60.0),
            Channel.EMAIL: RateLimitConfig(max_count=5, window_seconds=60.0),
        },
        global_rate_limit=RateLimitConfig(max_count=10, window_seconds=60.0),
    )


@pytest.fixture
def service(
    config: NotificationConfig,
    contacts: ContactInfoStore,
    settings: NotificationSettings,
    template_registry: TemplateRegistry,
) -> NotificationService:
    """Fully wired NotificationService for testing."""
    return NotificationService(
        config=config,
        contacts=contacts,
        settings=settings,
        template_registry=template_registry,
    )


# ---------------------------------------------------------------------------
# Validation Tests
# ---------------------------------------------------------------------------


class TestValidation:
    """Tests for request validation (missing contact info)."""

    @pytest.mark.asyncio
    async def test_missing_contact_raises_validation_error(
        self, service: NotificationService
    ) -> None:
        """Sending to a user with no contact info raises ValidationError."""
        request = NotificationRequest(
            recipient_id="nonexistent_user",
            channel=Channel.EMAIL,
            title="Hello",
            body="World",
        )
        with pytest.raises(ValidationError, match="No contact info"):
            await service.send(request)

    @pytest.mark.asyncio
    async def test_missing_channel_endpoint_raises_validation_error(
        self, service: NotificationService
    ) -> None:
        """User exists but has no endpoint for the requested channel."""
        # user3 has no phone number
        request = NotificationRequest(
            recipient_id="user3",
            channel=Channel.SMS,
            title="Alert",
            body="Test SMS",
        )
        with pytest.raises(ValidationError, match="No contact info"):
            await service.send(request)

    @pytest.mark.asyncio
    async def test_missing_push_token_raises_validation_error(
        self, service: NotificationService
    ) -> None:
        """User exists but has no device tokens for push channel."""
        # user3 has empty device_tokens
        request = NotificationRequest(
            recipient_id="user3",
            channel=Channel.IOS_PUSH,
            title="Push",
            body="Test push",
        )
        with pytest.raises(ValidationError, match="No contact info"):
            await service.send(request)


# ---------------------------------------------------------------------------
# Preferences Tests
# ---------------------------------------------------------------------------


class TestPreferences:
    """Tests for user preference (opt-out) checking."""

    @pytest.mark.asyncio
    async def test_opted_out_user_raises_validation_error(
        self, service: NotificationService, settings: NotificationSettings
    ) -> None:
        """Sending to a user who opted out raises ValidationError."""
        settings.opt_out("user1", Channel.EMAIL)
        request = NotificationRequest(
            recipient_id="user1",
            channel=Channel.EMAIL,
            title="Promo",
            body="Special offer",
        )
        with pytest.raises(ValidationError, match="opted out"):
            await service.send(request)

    @pytest.mark.asyncio
    async def test_opted_in_user_succeeds(
        self, service: NotificationService
    ) -> None:
        """Sending to a user who is opted in succeeds."""
        request = NotificationRequest(
            recipient_id="user1",
            channel=Channel.EMAIL,
            title="Hello",
            body="World",
        )
        notification_id = await service.send(request)
        assert notification_id  # non-empty string


# ---------------------------------------------------------------------------
# Rate Limiting Tests
# ---------------------------------------------------------------------------


class TestRateLimiting:
    """Tests for rate limit enforcement."""

    @pytest.mark.asyncio
    async def test_exceeding_per_channel_rate_limit(
        self, service: NotificationService
    ) -> None:
        """Exceeding per-channel rate limit raises RateLimitExceededError."""
        # SMS limit is 2 per 60s
        for i in range(2):
            request = NotificationRequest(
                recipient_id="user1",
                channel=Channel.SMS,
                title=f"SMS {i}",
                body=f"Body {i}",
            )
            await service.send(request)

        # Third SMS should be rate limited
        request = NotificationRequest(
            recipient_id="user1",
            channel=Channel.SMS,
            title="SMS overflow",
            body="Should fail",
        )
        with pytest.raises(RateLimitExceededError):
            await service.send(request)

    @pytest.mark.asyncio
    async def test_rate_limit_per_user_isolation(
        self, service: NotificationService
    ) -> None:
        """Rate limits are per-user; different users have independent limits."""
        # Send 2 SMS to user1 (hits limit)
        for i in range(2):
            request = NotificationRequest(
                recipient_id="user1",
                channel=Channel.SMS,
                title=f"SMS {i}",
                body=f"Body {i}",
            )
            await service.send(request)

        # user2 should still be able to send SMS
        request = NotificationRequest(
            recipient_id="user2",
            channel=Channel.SMS,
            title="SMS for user2",
            body="Should succeed",
        )
        notification_id = await service.send(request)
        assert notification_id


# ---------------------------------------------------------------------------
# Deduplication Tests
# ---------------------------------------------------------------------------


class TestDeduplication:
    """Tests for duplicate notification suppression."""

    @pytest.mark.asyncio
    async def test_duplicate_content_raises_error(
        self, service: NotificationService
    ) -> None:
        """Sending identical content twice raises DuplicateNotificationError."""
        request = NotificationRequest(
            recipient_id="user1",
            channel=Channel.EMAIL,
            title="Same Title",
            body="Same Body",
        )
        # First send succeeds
        await service.send(request)

        # Second send with same content should be deduplicated
        with pytest.raises(DuplicateNotificationError):
            await service.send(request)

    @pytest.mark.asyncio
    async def test_different_content_not_deduplicated(
        self, service: NotificationService
    ) -> None:
        """Different content to the same user is not deduplicated."""
        request1 = NotificationRequest(
            recipient_id="user1",
            channel=Channel.EMAIL,
            title="Title A",
            body="Body A",
        )
        request2 = NotificationRequest(
            recipient_id="user1",
            channel=Channel.EMAIL,
            title="Title B",
            body="Body B",
        )
        id1 = await service.send(request1)
        id2 = await service.send(request2)
        assert id1 != id2

    @pytest.mark.asyncio
    async def test_same_content_different_channel_not_deduplicated(
        self, service: NotificationService
    ) -> None:
        """Same content on different channels is not deduplicated."""
        request_email = NotificationRequest(
            recipient_id="user1",
            channel=Channel.EMAIL,
            title="Hello",
            body="World",
        )
        request_push = NotificationRequest(
            recipient_id="user1",
            channel=Channel.IOS_PUSH,
            title="Hello",
            body="World",
        )
        id1 = await service.send(request_email)
        id2 = await service.send(request_push)
        assert id1 != id2


# ---------------------------------------------------------------------------
# Successful Enqueue Tests
# ---------------------------------------------------------------------------


class TestSuccessfulEnqueue:
    """Tests for successful notification enqueue."""

    @pytest.mark.asyncio
    async def test_send_returns_unique_id(
        self, service: NotificationService
    ) -> None:
        """send() returns a unique notification ID (uuid4 hex)."""
        request = NotificationRequest(
            recipient_id="user1",
            channel=Channel.EMAIL,
            title="Test",
            body="Notification",
        )
        notification_id = await service.send(request)
        assert isinstance(notification_id, str)
        assert len(notification_id) == 32  # uuid4 hex is 32 chars

    @pytest.mark.asyncio
    async def test_multiple_sends_return_unique_ids(
        self, service: NotificationService
    ) -> None:
        """Multiple sends return distinct notification IDs."""
        ids = set()
        for i in range(5):
            request = NotificationRequest(
                recipient_id="user1",
                channel=Channel.EMAIL,
                title=f"Test {i}",
                body=f"Body {i}",
            )
            nid = await service.send(request)
            ids.add(nid)
        assert len(ids) == 5

    @pytest.mark.asyncio
    async def test_enqueued_task_appears_in_queue(
        self, service: NotificationService
    ) -> None:
        """After send(), the task is in the channel queue."""
        request = NotificationRequest(
            recipient_id="user1",
            channel=Channel.EMAIL,
            title="Queued",
            body="Check queue",
        )
        await service.send(request)
        assert service.queue.depth(Channel.EMAIL) == 1

    @pytest.mark.asyncio
    async def test_task_routed_to_correct_channel_queue(
        self, service: NotificationService
    ) -> None:
        """Task is enqueued to the correct channel queue."""
        request = NotificationRequest(
            recipient_id="user1",
            channel=Channel.IOS_PUSH,
            title="Push",
            body="Test",
        )
        await service.send(request)
        assert service.queue.depth(Channel.IOS_PUSH) == 1
        assert service.queue.depth(Channel.EMAIL) == 0


# ---------------------------------------------------------------------------
# Batch Send Tests
# ---------------------------------------------------------------------------


class TestBatchSend:
    """Tests for send_batch() functionality."""

    @pytest.mark.asyncio
    async def test_batch_send_all_succeed(
        self, service: NotificationService
    ) -> None:
        """Batch send with valid requests returns all IDs."""
        requests = [
            NotificationRequest(
                recipient_id="user1",
                channel=Channel.EMAIL,
                title=f"Batch {i}",
                body=f"Body {i}",
            )
            for i in range(3)
        ]
        results = await service.send_batch(requests)
        assert len(results) == 3
        assert all(r != "" for r in results)
        assert len(set(results)) == 3  # all unique

    @pytest.mark.asyncio
    async def test_batch_send_partial_failure(
        self, service: NotificationService
    ) -> None:
        """Batch send with some invalid requests returns empty strings for failures."""
        requests = [
            NotificationRequest(
                recipient_id="user1",
                channel=Channel.EMAIL,
                title="Valid",
                body="OK",
            ),
            NotificationRequest(
                recipient_id="nonexistent",
                channel=Channel.EMAIL,
                title="Invalid",
                body="No contact",
            ),
            NotificationRequest(
                recipient_id="user2",
                channel=Channel.EMAIL,
                title="Also valid",
                body="OK too",
            ),
        ]
        results = await service.send_batch(requests)
        assert len(results) == 3
        assert results[0] != ""  # success
        assert results[1] == ""  # failure (no contact)
        assert results[2] != ""  # success

    @pytest.mark.asyncio
    async def test_batch_send_empty_list(
        self, service: NotificationService
    ) -> None:
        """Batch send with empty list returns empty list."""
        results = await service.send_batch([])
        assert results == []


# ---------------------------------------------------------------------------
# Template Resolution Tests
# ---------------------------------------------------------------------------


class TestTemplateResolution:
    """Tests for template-based content resolution."""

    @pytest.mark.asyncio
    async def test_template_resolution_success(
        self, service: NotificationService
    ) -> None:
        """Sending with a valid template_id resolves content correctly."""
        request = NotificationRequest(
            recipient_id="user1",
            channel=Channel.EMAIL,
            template_id="welcome",
            template_params={"name": "Alice", "app": "TestApp"},
        )
        notification_id = await service.send(request)
        assert notification_id

        # Verify the task in the queue has the correct content
        task = service.queue.dequeue_nowait(Channel.EMAIL)
        assert task is not None
        assert task.notification_id == notification_id

    @pytest.mark.asyncio
    async def test_template_not_found_raises_validation_error(
        self, service: NotificationService
    ) -> None:
        """Sending with a non-existent template_id raises ValidationError."""
        request = NotificationRequest(
            recipient_id="user1",
            channel=Channel.EMAIL,
            template_id="nonexistent_template",
            template_params={},
        )
        with pytest.raises(ValidationError, match="Template.*not found"):
            await service.send(request)

    @pytest.mark.asyncio
    async def test_direct_content_without_template(
        self, service: NotificationService
    ) -> None:
        """Sending with direct title/body (no template) works correctly."""
        request = NotificationRequest(
            recipient_id="user1",
            channel=Channel.EMAIL,
            title="Direct Title",
            body="Direct Body",
        )
        notification_id = await service.send(request)
        assert notification_id
        assert service.queue.depth(Channel.EMAIL) == 1

    @pytest.mark.asyncio
    async def test_template_missing_params_raises_error(
        self, service: NotificationService
    ) -> None:
        """Template with missing required params raises TemplateRenderError."""
        from notification_system.exceptions import TemplateRenderError

        request = NotificationRequest(
            recipient_id="user1",
            channel=Channel.EMAIL,
            template_id="welcome",
            template_params={"name": "Alice"},  # missing 'app'
        )
        with pytest.raises(TemplateRenderError):
            await service.send(request)


# ---------------------------------------------------------------------------
# Event Tracking Integration Tests
# ---------------------------------------------------------------------------


class TestEventTracking:
    """Tests for lifecycle event tracking during the pipeline."""

    @pytest.mark.asyncio
    async def test_successful_send_records_created_and_queued(
        self, service: NotificationService
    ) -> None:
        """Successful send records CREATED and QUEUED events."""
        request = NotificationRequest(
            recipient_id="user1",
            channel=Channel.EMAIL,
            title="Track",
            body="Events",
        )
        notification_id = await service.send(request)
        history = service.tracker.get_history(notification_id)
        statuses = [e.status.value for e in history]
        assert "created" in statuses
        assert "queued" in statuses

    @pytest.mark.asyncio
    async def test_opted_out_records_unsubscribed_event(
        self, service: NotificationService, settings: NotificationSettings
    ) -> None:
        """Opted-out rejection records UNSUBSCRIBED event."""
        settings.opt_out("user1", Channel.EMAIL)
        request = NotificationRequest(
            recipient_id="user1",
            channel=Channel.EMAIL,
            title="Promo",
            body="Offer",
        )
        with pytest.raises(ValidationError):
            await service.send(request)

        # The tracker should have recorded an UNSUBSCRIBED event.
        # Since the notification_id is generated internally, we check
        # that at least one notification has UNSUBSCRIBED as its status.
        from notification_system.models import NotificationStatus

        found_unsubscribed = False
        for events in service.tracker._events.values():
            if any(e.status == NotificationStatus.UNSUBSCRIBED for e in events):
                found_unsubscribed = True
                break
        assert found_unsubscribed
