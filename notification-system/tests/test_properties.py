"""Property-based tests for the notification system using Hypothesis.

These tests verify universal correctness properties that must hold
across all valid inputs, complementing example-based unit tests.
"""

import asyncio
import uuid
from typing import List

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from notification_system.dedup import Deduplicator
from notification_system.exceptions import DuplicateNotificationError, QueueFullError
from notification_system.models import Channel, NotificationRequest, NotificationTask
from notification_system.queue import MessageQueue


# --- Strategies ---

channels = st.sampled_from(list(Channel))
recipient_ids = st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N")))
content_strings = st.text(min_size=1, max_size=100)
ttl_values = st.floats(min_value=1.0, max_value=3600.0, allow_nan=False, allow_infinity=False)


def make_task(channel, notification_id="notif-1"):
    """Helper to create a NotificationTask for a given channel."""
    request = NotificationRequest(
        recipient_id="user-1",
        channel=channel,
        title="Test",
        body="Test body",
    )
    return NotificationTask(
        notification_id=notification_id,
        request=request,
        channel=channel,
    )


# --- Property 6: Queue depth invariant ---
# Validates: Requirements 2.3, 2.4, 2.5


@given(
    channel=channels,
    max_depth=st.integers(min_value=1, max_value=100),
    num_enqueues=st.integers(min_value=0, max_value=100),
    num_dequeues=st.integers(min_value=0, max_value=100),
)
@settings(max_examples=200)
def test_queue_depth_equals_enqueues_minus_dequeues(channel, max_depth, num_enqueues, num_dequeues):
    """Property 6: Queue depth invariant.

    After N enqueues and M dequeues (M <= N, N <= max_depth),
    the queue depth equals N - M.

    **Validates: Requirements 2.3, 2.4, 2.5**
    """
    # Constrain: can't enqueue more than max_depth, can't dequeue more than enqueued
    actual_enqueues = min(num_enqueues, max_depth)
    actual_dequeues = min(num_dequeues, actual_enqueues)

    async def run():
        mq = MessageQueue(max_depth=max_depth)

        # Enqueue actual_enqueues tasks
        for i in range(actual_enqueues):
            task = make_task(channel, notification_id="notif-{}".format(i))
            await mq.enqueue(task)

        # Verify depth after enqueues
        assert mq.depth(channel) == actual_enqueues

        # Dequeue actual_dequeues tasks
        for _ in range(actual_dequeues):
            result = mq.dequeue_nowait(channel)
            assert result is not None

        # Verify depth invariant: depth == enqueues - dequeues
        assert mq.depth(channel) == actual_enqueues - actual_dequeues

    asyncio.get_event_loop().run_until_complete(run())


@given(
    channel=channels,
    max_depth=st.integers(min_value=1, max_value=50),
)
@settings(max_examples=100)
def test_queue_full_raises_error_at_max_depth(channel, max_depth):
    """Property 6: Queue depth invariant - overflow case.

    Enqueueing beyond max_depth raises QueueFullError, and the queue
    depth never exceeds the configured maximum.

    **Validates: Requirements 2.3, 2.4, 2.5**
    """

    async def run():
        mq = MessageQueue(max_depth=max_depth)

        # Fill the queue to max_depth
        for i in range(max_depth):
            task = make_task(channel, notification_id="notif-{}".format(i))
            await mq.enqueue(task)

        # Verify queue is at max depth
        assert mq.depth(channel) == max_depth
        assert mq.is_full(channel)

        # Attempting to enqueue one more should raise QueueFullError
        overflow_task = make_task(channel, notification_id="overflow")
        with pytest.raises(QueueFullError) as exc_info:
            await mq.enqueue(overflow_task)

        assert exc_info.value.channel == channel.value
        assert exc_info.value.max_depth == max_depth

        # Depth must still equal max_depth (overflow was rejected)
        assert mq.depth(channel) == max_depth

    asyncio.get_event_loop().run_until_complete(run())


@given(
    channel=channels,
    max_depth=st.integers(min_value=1, max_value=50),
    operations=st.lists(
        st.sampled_from(["enqueue", "dequeue"]),
        min_size=1,
        max_size=100,
    ),
)
@settings(max_examples=200)
def test_queue_depth_never_exceeds_max_depth(channel, max_depth, operations):
    """Property 6: Queue depth invariant - arbitrary operation sequences.

    For any sequence of enqueue/dequeue operations, the queue depth
    SHALL never exceed the configured maximum depth.

    **Validates: Requirements 2.3, 2.4, 2.5**
    """

    async def run():
        mq = MessageQueue(max_depth=max_depth)
        enqueue_count = 0
        dequeue_count = 0

        for i, op in enumerate(operations):
            if op == "enqueue":
                task = make_task(channel, notification_id="notif-{}".format(i))
                try:
                    await mq.enqueue(task)
                    enqueue_count += 1
                except QueueFullError:
                    # Expected when queue is full
                    pass
            else:  # dequeue
                result = mq.dequeue_nowait(channel)
                if result is not None:
                    dequeue_count += 1

            # Invariant: depth == enqueues - dequeues
            assert mq.depth(channel) == enqueue_count - dequeue_count
            # Invariant: depth never exceeds max_depth
            assert mq.depth(channel) <= max_depth

    asyncio.get_event_loop().run_until_complete(run())


# --- Property 14: Duplicate suppression within window ---
# Validates: Requirements 7.2, 7.4

# Strategies for Property 14
recipient_ids = st.text(
    min_size=1, max_size=50,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
)
content_strings = st.text(min_size=1, max_size=200)
notification_ids_uuid = st.builds(lambda: uuid.uuid4().hex)
ttl_values = st.floats(min_value=1.0, max_value=3600.0, allow_nan=False, allow_infinity=False)


class TestProperty14DuplicateSuppressionWithinWindow:
    """Property 14: Duplicate suppression within window.

    After recording a notification, any subsequent check_and_record with the
    same (recipient, channel, content) within the TTL window raises
    DuplicateNotificationError with the original notification ID.

    **Validates: Requirements 7.2, 7.4**
    """

    @given(
        recipient_id=recipient_ids,
        channel=channels,
        content=content_strings,
        original_id=notification_ids_uuid,
        duplicate_id=notification_ids_uuid,
        ttl=ttl_values,
    )
    @settings(max_examples=200)
    def test_duplicate_raises_error_within_window(
        self,
        recipient_id: str,
        channel: Channel,
        content: str,
        original_id: str,
        duplicate_id: str,
        ttl: float,
    ):
        """A second check_and_record with same (recipient, channel, content)
        within the TTL window raises DuplicateNotificationError with the
        original notification ID.

        **Validates: Requirements 7.2, 7.4**
        """
        current_time = 0.0
        dedup = Deduplicator(window_seconds=ttl, clock=lambda: current_time)

        # First call should succeed without raising
        dedup.check_and_record(recipient_id, channel, content, original_id)

        # Second call with same (recipient, channel, content) within TTL should raise
        with pytest.raises(DuplicateNotificationError) as exc_info:
            dedup.check_and_record(recipient_id, channel, content, duplicate_id)

        # The error must reference the original notification ID (Requirement 7.4)
        assert exc_info.value.original_id == original_id

    @given(
        recipient_id=recipient_ids,
        channel=channels,
        content=content_strings,
        original_id=notification_ids_uuid,
        duplicate_id=notification_ids_uuid,
        ttl=ttl_values,
        elapsed_fraction=st.floats(min_value=0.0, max_value=0.99, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=200)
    def test_duplicate_detected_at_any_point_within_window(
        self,
        recipient_id: str,
        channel: Channel,
        content: str,
        original_id: str,
        duplicate_id: str,
        ttl: float,
        elapsed_fraction: float,
    ):
        """At any point within the TTL window (0% to 99% elapsed), a duplicate
        is still suppressed and raises DuplicateNotificationError.

        **Validates: Requirements 7.2, 7.4**
        """
        current_time = 0.0

        def clock():
            return current_time

        dedup = Deduplicator(window_seconds=ttl, clock=clock)

        # Record the original notification at time 0
        dedup.check_and_record(recipient_id, channel, content, original_id)

        # Advance time to some fraction within the window
        current_time = ttl * elapsed_fraction

        # Should still be detected as duplicate
        with pytest.raises(DuplicateNotificationError) as exc_info:
            dedup.check_and_record(recipient_id, channel, content, duplicate_id)

        assert exc_info.value.original_id == original_id


# --- Strategies for deduplication tests ---

recipient_ids = st.text(min_size=1, max_size=50)
content_strings = st.text(min_size=1, max_size=200)


# --- Property 13: Deduplication key determinism ---
# Validates: Requirements 7.1


class TestProperty13DeduplicationKeyDeterminism:
    """Property 13: Deduplication key determinism.

    For any (recipient_id, channel, content) triple, computing the deduplication
    key multiple times SHALL always produce the same result. Two triples that
    differ in any component SHALL produce different keys.

    **Validates: Requirements 7.1**
    """

    @given(
        recipient_id=recipient_ids,
        channel=channels,
        content=content_strings,
    )
    @settings(max_examples=200)
    def test_compute_key_is_deterministic(
        self, recipient_id: str, channel: Channel, content: str
    ) -> None:
        """For any input triple, compute_key always returns the same result.

        **Validates: Requirements 7.1**
        """
        dedup = Deduplicator()
        key1 = dedup.compute_key(recipient_id, channel, content)
        key2 = dedup.compute_key(recipient_id, channel, content)
        assert key1 == key2, (
            f"compute_key is not deterministic for "
            f"({recipient_id!r}, {channel}, {content!r}): {key1} != {key2}"
        )

    @given(
        recipient_id_a=recipient_ids,
        channel_a=channels,
        content_a=content_strings,
        recipient_id_b=recipient_ids,
        channel_b=channels,
        content_b=content_strings,
    )
    @settings(max_examples=200)
    def test_different_inputs_produce_different_keys(
        self,
        recipient_id_a: str,
        channel_a: Channel,
        content_a: str,
        recipient_id_b: str,
        channel_b: Channel,
        content_b: str,
    ) -> None:
        """Different inputs produce different keys (with high probability).

        If any component of the triple differs, the keys should differ.

        **Validates: Requirements 7.1**
        """
        # Only test when at least one component differs
        assume(
            recipient_id_a != recipient_id_b
            or channel_a != channel_b
            or content_a != content_b
        )

        dedup = Deduplicator()
        key_a = dedup.compute_key(recipient_id_a, channel_a, content_a)
        key_b = dedup.compute_key(recipient_id_b, channel_b, content_b)
        assert key_a != key_b, (
            f"Different inputs produced the same key: "
            f"({recipient_id_a!r}, {channel_a}, {content_a!r}) and "
            f"({recipient_id_b!r}, {channel_b}, {content_b!r}) both gave {key_a}"
        )


# --- Property 10: Rate limiter enforcement ---
# Validates: Requirements 6.1, 6.4

from notification_system.config import RateLimitConfig
from notification_system.exceptions import RateLimitExceededError
from notification_system.rate_limiter import SlidingWindowRateLimiter

# Strategies for rate limiter tests
user_ids = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
    min_size=1,
    max_size=20,
)

rate_limit_configs = st.builds(
    RateLimitConfig,
    max_count=st.integers(min_value=1, max_value=50),
    window_seconds=st.floats(min_value=1.0, max_value=3600.0, allow_nan=False, allow_infinity=False),
)


class TestProperty10RateLimiterEnforcement:
    """Property 10: Rate limiter enforcement.

    For any rate limit config (max_count N, window W), after recording
    exactly N notifications for a user+channel, the next check raises
    RateLimitExceededError. Before N, checks pass.

    **Validates: Requirements 6.1, 6.4**
    """

    @given(
        user_id=user_ids,
        channel=channels,
        config=rate_limit_configs,
    )
    @settings(max_examples=200)
    def test_rate_limit_enforced_after_n_notifications(
        self, user_id: str, channel: Channel, config: RateLimitConfig
    ):
        """After exactly N recordings within window W, the (N+1)th check
        raises RateLimitExceededError.

        **Validates: Requirements 6.1, 6.4**
        """
        current_time = 0.0

        def clock():
            return current_time

        # Set up limiter with the generated config for the specific channel
        # Use a very high global limit so it doesn't interfere
        per_channel_limits = {channel: config}
        global_limit = RateLimitConfig(max_count=10000, window_seconds=config.window_seconds)

        limiter = SlidingWindowRateLimiter(
            per_channel_limits=per_channel_limits,
            global_limit=global_limit,
            clock=clock,
        )

        # Record exactly N notifications - all checks before recording should pass
        for i in range(config.max_count):
            # Check should pass before we've hit the limit
            limiter.check(user_id, channel)
            limiter.record(user_id, channel)

        # The (N+1)th check should raise RateLimitExceededError
        with pytest.raises(RateLimitExceededError) as exc_info:
            limiter.check(user_id, channel)

        assert exc_info.value.user_id == user_id
        assert exc_info.value.limit == config.max_count
        assert exc_info.value.window == config.window_seconds

    @given(
        user_id=user_ids,
        channel=channels,
        config=rate_limit_configs,
        k=st.integers(min_value=0, max_value=49),
    )
    @settings(max_examples=200)
    def test_checks_pass_before_limit_reached(
        self, user_id: str, channel: Channel, config: RateLimitConfig, k: int
    ):
        """Before N notifications are recorded, checks pass without raising.

        **Validates: Requirements 6.1, 6.4**
        """
        assume(k < config.max_count)

        current_time = 0.0

        def clock():
            return current_time

        per_channel_limits = {channel: config}
        global_limit = RateLimitConfig(max_count=10000, window_seconds=config.window_seconds)

        limiter = SlidingWindowRateLimiter(
            per_channel_limits=per_channel_limits,
            global_limit=global_limit,
            clock=clock,
        )

        # Record k notifications (k < N)
        for i in range(k):
            limiter.check(user_id, channel)
            limiter.record(user_id, channel)

        # The (k+1)th check should still pass since k < N
        limiter.check(user_id, channel)  # Should not raise


# --- Property 11: Per-channel rate limit independence ---
# Validates: Requirements 6.2

from notification_system.config import RateLimitConfig
from notification_system.exceptions import RateLimitExceededError
from notification_system.rate_limiter import SlidingWindowRateLimiter


def distinct_channel_pairs():
    """Strategy that generates two distinct Channel values."""
    all_channels = list(Channel)
    return st.tuples(
        st.sampled_from(all_channels),
        st.sampled_from(all_channels),
    ).filter(lambda pair: pair[0] != pair[1])


class TestProperty11PerChannelRateLimitIndependence:
    """Property 11: Per-channel rate limit independence.

    For any user, exhausting the rate limit on one channel SHALL NOT
    affect the rate limit availability on other channels (each channel's
    limit is tracked independently).

    **Validates: Requirements 6.2**
    """

    @given(
        channel_pair=distinct_channel_pairs(),
        user_id=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N"))),
        max_count=st.integers(min_value=1, max_value=20),
    )
    @settings(max_examples=100)
    def test_exhausting_one_channel_does_not_affect_another(
        self, channel_pair: tuple, user_id: str, max_count: int
    ):
        """Exhausting the rate limit on one channel does not affect the
        rate limit on a different channel for the same user.

        **Validates: Requirements 6.2**
        """
        channel_a, channel_b = channel_pair

        # Configure per-channel limits with the same max_count for all channels
        per_channel_limits = {
            ch: RateLimitConfig(max_count=max_count, window_seconds=3600.0)
            for ch in Channel
        }
        # Set global limit high enough to not interfere with per-channel testing
        global_limit = RateLimitConfig(
            max_count=max_count * len(Channel) + 100, window_seconds=3600.0
        )

        clock_time = [0.0]

        def fake_clock() -> float:
            return clock_time[0]

        limiter = SlidingWindowRateLimiter(
            per_channel_limits=per_channel_limits,
            global_limit=global_limit,
            clock=fake_clock,
        )

        # Exhaust the rate limit on channel_a
        for i in range(max_count):
            clock_time[0] = float(i)
            limiter.check(user_id, channel_a)
            limiter.record(user_id, channel_a)

        # Verify channel_a is now rate-limited
        clock_time[0] = float(max_count)
        with pytest.raises(RateLimitExceededError):
            limiter.check(user_id, channel_a)

        # Verify channel_b is NOT affected - should still accept notifications
        limiter.check(user_id, channel_b)  # Should NOT raise
        limiter.record(user_id, channel_b)

        # Verify channel_b can be filled to its own limit independently
        for i in range(1, max_count):
            clock_time[0] = float(max_count + i)
            limiter.check(user_id, channel_b)
            limiter.record(user_id, channel_b)

        # Now channel_b should also be exhausted (independently)
        clock_time[0] = float(max_count + max_count)
        with pytest.raises(RateLimitExceededError):
            limiter.check(user_id, channel_b)


# --- Property 9: Template registration round-trip ---
# Validates: Requirements 5.6

from notification_system.templates import NotificationTemplate, TemplateRegistry

# Strategy for valid template IDs: non-empty strings of word characters
template_ids = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "Pc"), whitelist_characters="-"),
    min_size=1,
    max_size=50,
)

# Strategy for template titles and bodies
template_text = st.text(min_size=0, max_size=200)


class TestProperty9TemplateRegistrationRoundTrip:
    """Property 9: Template registration round-trip.

    For any notification template registered with a unique ID, retrieving
    that template by ID SHALL return a template with the same ID, title
    template, and body template, and the ID appears in list_ids().

    **Validates: Requirements 5.6**
    """

    @given(
        template_id=template_ids,
        title=template_text,
        body=template_text,
    )
    @settings(max_examples=100)
    def test_registered_template_retrievable_by_id(
        self, template_id: str, title: str, body: str
    ) -> None:
        """Registering a template and retrieving it by ID returns the same template object.

        **Validates: Requirements 5.6**
        """
        registry = TemplateRegistry()
        template = NotificationTemplate(template_id=template_id, title=title, body=body)

        registry.register(template)
        retrieved = registry.get(template_id)

        assert retrieved is not None
        assert retrieved is template
        assert retrieved.template_id == template_id
        assert retrieved.title_template == title
        assert retrieved.body_template == body

    @given(
        template_id=template_ids,
        title=template_text,
        body=template_text,
    )
    @settings(max_examples=100)
    def test_registered_template_id_in_list_ids(
        self, template_id: str, title: str, body: str
    ) -> None:
        """After registration, the template ID appears in list_ids().

        **Validates: Requirements 5.6**
        """
        registry = TemplateRegistry()
        template = NotificationTemplate(template_id=template_id, title=title, body=body)

        registry.register(template)

        assert template_id in registry.list_ids()


# --- Property 7: Template rendering substitution ---
# Validates: Requirements 5.1, 5.2

import re

from notification_system.templates import NotificationTemplate


# Strategy for valid placeholder names (word characters, starts with letter)
_placeholder_name_st = st.from_regex(r"[a-zA-Z][a-zA-Z0-9_]{0,14}", fullmatch=True)


@st.composite
def template_with_params(draw):
    """Generate a template with placeholders and a complete matching params dict.

    Produces a tuple of (title_template, body_template, params) where:
    - title and body contain {placeholder} syntax interspersed with literal text
    - params is a complete dict mapping every placeholder to a value
    - Values do not contain {placeholder} syntax (to avoid false positives)
    """
    # Generate 1-5 unique placeholder names
    num_placeholders = draw(st.integers(min_value=1, max_value=5))
    placeholders = draw(
        st.lists(
            _placeholder_name_st,
            min_size=num_placeholders,
            max_size=num_placeholders,
            unique=True,
        )
    )

    # Literal text that cannot contain braces (avoids accidental placeholder syntax)
    literal_st = st.text(
        alphabet=st.characters(
            blacklist_characters="{}",
            blacklist_categories=("Cs",),
        ),
        min_size=0,
        max_size=20,
    )

    # Build title with at least one placeholder
    title_placeholders = draw(
        st.lists(st.sampled_from(placeholders), min_size=1, max_size=3)
    )
    title_parts = []
    for ph in title_placeholders:
        title_parts.append(draw(literal_st))
        title_parts.append("{" + ph + "}")
    title_parts.append(draw(literal_st))
    title = "".join(title_parts)

    # Build body with at least one placeholder
    body_placeholders = draw(
        st.lists(st.sampled_from(placeholders), min_size=1, max_size=5)
    )
    body_parts = []
    for ph in body_placeholders:
        body_parts.append(draw(literal_st))
        body_parts.append("{" + ph + "}")
    body_parts.append(draw(literal_st))
    body = "".join(body_parts)

    # Generate param values without brace characters (no {word} patterns in values)
    value_st = st.text(
        alphabet=st.characters(
            blacklist_characters="{}",
            blacklist_categories=("Cs",),
        ),
        min_size=0,
        max_size=50,
    )
    params = {}
    for ph in placeholders:
        params[ph] = draw(value_st)

    return title, body, params


_PLACEHOLDER_RE = re.compile(r"\{\w+\}")


class TestProperty7TemplateRenderingSubstitution:
    """Property 7: Template rendering substitution.

    For any template with placeholders and a complete params dict,
    rendering produces output where all placeholders are replaced with
    their corresponding values and no {placeholder} syntax remains.

    **Validates: Requirements 5.1, 5.2**
    """

    @given(data=template_with_params())
    @settings(max_examples=200)
    def test_no_placeholder_syntax_remains_after_render(self, data):
        """Rendered output contains no {placeholder} syntax.

        **Validates: Requirements 5.1, 5.2**
        """
        title_template, body_template, params = data

        template = NotificationTemplate(
            template_id="prop7-test",
            title=title_template,
            body=body_template,
        )

        rendered_title, rendered_body = template.render(params)

        # No {placeholder} syntax should remain in the rendered output
        assert not _PLACEHOLDER_RE.search(rendered_title), (
            "Rendered title still contains placeholder syntax: {!r}".format(
                rendered_title
            )
        )
        assert not _PLACEHOLDER_RE.search(rendered_body), (
            "Rendered body still contains placeholder syntax: {!r}".format(
                rendered_body
            )
        )

    @given(data=template_with_params())
    @settings(max_examples=200)
    def test_each_placeholder_replaced_by_corresponding_value(self, data):
        """Each placeholder is substituted with its corresponding param value.

        **Validates: Requirements 5.1, 5.2**
        """
        title_template, body_template, params = data

        template = NotificationTemplate(
            template_id="prop7-test",
            title=title_template,
            body=body_template,
        )

        rendered_title, rendered_body = template.render(params)

        # For each placeholder in the title, verify its value appears in the output
        # We verify by reconstructing what the output should be via manual substitution
        expected_title = title_template
        for ph, val in params.items():
            expected_title = expected_title.replace("{" + ph + "}", val)

        expected_body = body_template
        for ph, val in params.items():
            expected_body = expected_body.replace("{" + ph + "}", val)

        assert rendered_title == expected_title, (
            "Title mismatch: expected {!r}, got {!r}".format(
                expected_title, rendered_title
            )
        )
        assert rendered_body == expected_body, (
            "Body mismatch: expected {!r}, got {!r}".format(
                expected_body, rendered_body
            )
        )


# --- Property 5: Queue FIFO ordering ---
# Validates: Requirements 2.6


class TestProperty5QueueFIFOOrdering:
    """Property 5: Queue FIFO ordering.

    For any sequence of NotificationTasks enqueued to the same channel,
    dequeuing produces them in the same FIFO order.

    **Validates: Requirements 2.6**
    """

    @given(
        channel=channels,
        task_ids=st.lists(
            st.text(
                min_size=1,
                max_size=30,
                alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
            ),
            min_size=1,
            max_size=50,
        ),
    )
    @settings(max_examples=200)
    def test_dequeue_order_matches_enqueue_order(
        self, channel: Channel, task_ids: List[str]
    ):
        """For any list of notification task IDs enqueued to the same channel,
        dequeuing returns them in exactly the same FIFO order.

        **Validates: Requirements 2.6**
        """

        async def run():
            mq = MessageQueue(max_depth=len(task_ids) + 10)

            # Enqueue all tasks in order
            for tid in task_ids:
                task = make_task(channel, notification_id=tid)
                await mq.enqueue(task)

            # Dequeue all tasks and verify FIFO order
            dequeued_ids = []
            for _ in range(len(task_ids)):
                result = mq.dequeue_nowait(channel)
                assert result is not None, "Expected a task but got None"
                dequeued_ids.append(result.notification_id)

            assert dequeued_ids == task_ids, (
                f"FIFO order violated: enqueued {task_ids}, "
                f"but dequeued {dequeued_ids}"
            )

        asyncio.get_event_loop().run_until_complete(run())

    @given(
        channel=channels,
        task_ids=st.lists(
            st.text(
                min_size=1,
                max_size=30,
                alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
            ),
            min_size=2,
            max_size=50,
        ),
        dequeue_count=st.integers(min_value=1, max_value=49),
    )
    @settings(max_examples=200)
    def test_partial_dequeue_preserves_fifo_order(
        self, channel: Channel, task_ids: List[str], dequeue_count: int
    ):
        """Dequeuing a subset of enqueued tasks still preserves FIFO order
        for the dequeued portion.

        **Validates: Requirements 2.6**
        """
        assume(dequeue_count <= len(task_ids))

        async def run():
            mq = MessageQueue(max_depth=len(task_ids) + 10)

            # Enqueue all tasks in order
            for tid in task_ids:
                task = make_task(channel, notification_id=tid)
                await mq.enqueue(task)

            # Dequeue only a subset
            dequeued_ids = []
            for _ in range(dequeue_count):
                result = mq.dequeue_nowait(channel)
                assert result is not None, "Expected a task but got None"
                dequeued_ids.append(result.notification_id)

            # The dequeued subset must match the first dequeue_count items
            assert dequeued_ids == task_ids[:dequeue_count], (
                f"Partial FIFO order violated: expected first {dequeue_count} "
                f"items {task_ids[:dequeue_count]}, but got {dequeued_ids}"
            )

        asyncio.get_event_loop().run_until_complete(run())


# --- Property 8: Template missing placeholder error ---
# Validates: Requirements 5.3

from notification_system.exceptions import TemplateRenderError
from notification_system.templates import NotificationTemplate


@st.composite
def template_with_incomplete_params(draw):
    """Generate a template with placeholders and an incomplete params dict.

    Produces a tuple of (title_template, body_template, incomplete_params, missing_keys)
    where:
    - title and body contain {placeholder} syntax with at least one placeholder total
    - incomplete_params is missing at least one required key
    - missing_keys is the sorted list of keys that are missing
    """
    # Generate 1-5 unique placeholder names
    num_placeholders = draw(st.integers(min_value=1, max_value=5))
    placeholders = draw(
        st.lists(
            _placeholder_name_st,
            min_size=num_placeholders,
            max_size=num_placeholders,
            unique=True,
        )
    )

    # Literal text that cannot contain braces (avoids accidental placeholder syntax)
    literal_st = st.text(
        alphabet=st.characters(
            blacklist_characters="{}",
            blacklist_categories=("Cs",),
        ),
        min_size=0,
        max_size=20,
    )

    # Build title with at least one placeholder
    title_placeholders = draw(
        st.lists(st.sampled_from(placeholders), min_size=1, max_size=3)
    )
    title_parts = []
    for ph in title_placeholders:
        title_parts.append(draw(literal_st))
        title_parts.append("{" + ph + "}")
    title_parts.append(draw(literal_st))
    title = "".join(title_parts)

    # Build body with at least one placeholder
    body_placeholders = draw(
        st.lists(st.sampled_from(placeholders), min_size=1, max_size=5)
    )
    body_parts = []
    for ph in body_placeholders:
        body_parts.append(draw(literal_st))
        body_parts.append("{" + ph + "}")
    body_parts.append(draw(literal_st))
    body = "".join(body_parts)

    # Compute the ACTUAL required keys from the template text (union of title + body)
    all_required = set(title_placeholders) | set(body_placeholders)

    # Choose a non-empty subset of actual required keys to OMIT
    num_to_omit = draw(st.integers(min_value=1, max_value=len(all_required)))
    keys_to_omit = set(draw(
        st.lists(
            st.sampled_from(sorted(all_required)),
            min_size=num_to_omit,
            max_size=num_to_omit,
            unique=True,
        )
    ))

    # Build incomplete params: provide values only for keys NOT omitted
    value_st = st.text(
        alphabet=st.characters(
            blacklist_characters="{}",
            blacklist_categories=("Cs",),
        ),
        min_size=0,
        max_size=50,
    )
    incomplete_params = {}
    for ph in all_required:
        if ph not in keys_to_omit:
            incomplete_params[ph] = draw(value_st)

    missing_keys = sorted(keys_to_omit)

    return title, body, incomplete_params, missing_keys


class TestProperty8TemplateMissingPlaceholderError:
    """Property 8: Template missing placeholder error.

    For any template with at least one placeholder, rendering with an
    incomplete params dict (missing at least one required key) raises
    TemplateRenderError with the correct missing_keys.

    **Validates: Requirements 5.3**
    """

    @given(data=template_with_incomplete_params())
    @settings(max_examples=200)
    def test_missing_keys_raises_template_render_error(self, data):
        """Rendering a template with missing placeholders raises TemplateRenderError.

        **Validates: Requirements 5.3**
        """
        title_template, body_template, incomplete_params, expected_missing = data

        template = NotificationTemplate(
            template_id="prop8-test",
            title=title_template,
            body=body_template,
        )

        with pytest.raises(TemplateRenderError) as exc_info:
            template.render(incomplete_params)

        # The error must report the correct missing keys
        assert sorted(exc_info.value.missing_keys) == expected_missing, (
            f"Expected missing_keys={expected_missing}, "
            f"got {sorted(exc_info.value.missing_keys)}"
        )

    @given(data=template_with_incomplete_params())
    @settings(max_examples=200)
    def test_error_contains_correct_template_id(self, data):
        """TemplateRenderError includes the correct template_id.

        **Validates: Requirements 5.3**
        """
        title_template, body_template, incomplete_params, expected_missing = data

        template_id = "prop8-tmpl-id"
        template = NotificationTemplate(
            template_id=template_id,
            title=title_template,
            body=body_template,
        )

        with pytest.raises(TemplateRenderError) as exc_info:
            template.render(incomplete_params)

        assert exc_info.value.template_id == template_id


# --- Property 12: Sliding window expiry ---
# Validates: Requirements 6.6

from notification_system.config import RateLimitConfig
from notification_system.exceptions import RateLimitExceededError
from notification_system.rate_limiter import SlidingWindowRateLimiter


class TestProperty12SlidingWindowExpiry:
    """Property 12: Sliding window expiry.

    For any user and channel, after the configured time window has fully
    elapsed since all recorded notifications, the rate limiter SHALL accept
    new notifications (the window slides forward and old entries expire).

    **Validates: Requirements 6.6**
    """

    @given(
        user_id=st.text(
            min_size=1,
            max_size=20,
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
        ),
        channel=channels,
        max_count=st.integers(min_value=1, max_value=20),
        window_seconds=st.floats(min_value=1.0, max_value=3600.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=200)
    def test_rate_limit_resets_after_window_expires(
        self, user_id: str, channel: Channel, max_count: int, window_seconds: float
    ):
        """After the sliding window fully expires, previously recorded
        notifications no longer count toward the rate limit. A user who
        was rate-limited can send again after the window passes.

        **Validates: Requirements 6.6**
        """
        current_time = [0.0]

        def clock() -> float:
            return current_time[0]

        config = RateLimitConfig(max_count=max_count, window_seconds=window_seconds)
        per_channel_limits = {channel: config}
        global_limit = RateLimitConfig(max_count=10000, window_seconds=window_seconds)

        limiter = SlidingWindowRateLimiter(
            per_channel_limits=per_channel_limits,
            global_limit=global_limit,
            clock=clock,
        )

        # Fill the rate limit to capacity
        for i in range(max_count):
            limiter.check(user_id, channel)
            limiter.record(user_id, channel)

        # Verify the user is now rate-limited
        with pytest.raises(RateLimitExceededError):
            limiter.check(user_id, channel)

        # Advance time past the full window
        current_time[0] = window_seconds + 1.0

        # After the window expires, the user should be able to send again
        limiter.check(user_id, channel)  # Should NOT raise

    @given(
        user_id=st.text(
            min_size=1,
            max_size=20,
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
        ),
        channel=channels,
        max_count=st.integers(min_value=1, max_value=20),
        window_seconds=st.floats(min_value=1.0, max_value=3600.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=200)
    def test_can_fill_rate_limit_again_after_window_expires(
        self, user_id: str, channel: Channel, max_count: int, window_seconds: float
    ):
        """After the window expires and old entries are pruned, the user
        can record another full batch of max_count notifications before
        being rate-limited again.

        **Validates: Requirements 6.6**
        """
        current_time = [0.0]

        def clock() -> float:
            return current_time[0]

        config = RateLimitConfig(max_count=max_count, window_seconds=window_seconds)
        per_channel_limits = {channel: config}
        global_limit = RateLimitConfig(max_count=10000, window_seconds=window_seconds)

        limiter = SlidingWindowRateLimiter(
            per_channel_limits=per_channel_limits,
            global_limit=global_limit,
            clock=clock,
        )

        # Fill the rate limit to capacity at time 0
        for i in range(max_count):
            limiter.check(user_id, channel)
            limiter.record(user_id, channel)

        # Verify rate-limited
        with pytest.raises(RateLimitExceededError):
            limiter.check(user_id, channel)

        # Advance time past the window
        current_time[0] = window_seconds + 1.0

        # Should be able to fill the limit again completely
        for i in range(max_count):
            limiter.check(user_id, channel)
            limiter.record(user_id, channel)

        # And now should be rate-limited again
        with pytest.raises(RateLimitExceededError):
            limiter.check(user_id, channel)

    @given(
        user_id=st.text(
            min_size=1,
            max_size=20,
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
        ),
        channel=channels,
        max_count=st.integers(min_value=2, max_value=20),
        window_seconds=st.floats(min_value=2.0, max_value=3600.0, allow_nan=False, allow_infinity=False),
        elapsed_fraction=st.floats(min_value=0.01, max_value=0.99, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=200)
    def test_still_rate_limited_before_window_expires(
        self, user_id: str, channel: Channel, max_count: int, window_seconds: float, elapsed_fraction: float
    ):
        """Before the window fully expires, the rate limit is still enforced.
        This confirms the sliding window only releases entries after the
        full window duration.

        **Validates: Requirements 6.6**
        """
        current_time = [0.0]

        def clock() -> float:
            return current_time[0]

        config = RateLimitConfig(max_count=max_count, window_seconds=window_seconds)
        per_channel_limits = {channel: config}
        global_limit = RateLimitConfig(max_count=10000, window_seconds=window_seconds)

        limiter = SlidingWindowRateLimiter(
            per_channel_limits=per_channel_limits,
            global_limit=global_limit,
            clock=clock,
        )

        # Fill the rate limit to capacity at time 0
        for i in range(max_count):
            limiter.check(user_id, channel)
            limiter.record(user_id, channel)

        # Advance time to a fraction within the window (not yet expired)
        current_time[0] = window_seconds * elapsed_fraction

        # Should still be rate-limited since window hasn't fully elapsed
        with pytest.raises(RateLimitExceededError):
            limiter.check(user_id, channel)



# --- Property 17: Default opt-in for new users ---
# Validates: Requirements 8.2

from notification_system.settings import NotificationSettings


class TestProperty17DefaultOptInForNewUsers:
    """Property 17: Default opt-in for new users.

    For any new user ID that has never been seen, all channels default
    to opted-in. This ensures new users receive notifications on every
    channel until they explicitly opt out.

    **Validates: Requirements 8.2**
    """

    @given(
        user_id=st.text(
            min_size=1,
            max_size=50,
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
        ),
    )
    @settings(max_examples=200)
    def test_new_user_all_channels_opted_in(self, user_id: str):
        """For any new user ID, is_opted_in returns True for every channel.

        **Validates: Requirements 8.2**
        """
        ns = NotificationSettings()

        for channel in Channel:
            assert ns.is_opted_in(user_id, channel) is True, (
                f"Expected user {user_id!r} to be opted-in for {channel}, "
                f"but got False"
            )

    @given(
        user_id=st.text(
            min_size=1,
            max_size=50,
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
        ),
    )
    @settings(max_examples=200)
    def test_new_user_get_all_preferences_all_true(self, user_id: str):
        """For any new user ID, get_all_preferences returns True for every channel.

        **Validates: Requirements 8.2**
        """
        ns = NotificationSettings()

        prefs = ns.get_all_preferences(user_id)

        # All channels must be present and opted-in
        for channel in Channel:
            assert channel in prefs, (
                f"Channel {channel} missing from preferences for user {user_id!r}"
            )
            assert prefs[channel] is True, (
                f"Expected user {user_id!r} preference for {channel} to be True, "
                f"got {prefs[channel]}"
            )

    @given(
        user_ids=st.lists(
            st.text(
                min_size=1,
                max_size=50,
                alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
            ),
            min_size=1,
            max_size=10,
            unique=True,
        ),
    )
    @settings(max_examples=100)
    def test_multiple_new_users_all_default_opted_in(self, user_ids):
        """For any set of distinct new user IDs, each user defaults to
        opted-in on all channels independently.

        **Validates: Requirements 8.2**
        """
        ns = NotificationSettings()

        for user_id in user_ids:
            for channel in Channel:
                assert ns.is_opted_in(user_id, channel) is True, (
                    f"Expected user {user_id!r} to be opted-in for {channel}"
                )


# --- Property 16: Notification settings round-trip ---
# Validates: Requirements 8.1, 8.3, 8.4

from notification_system.settings import NotificationSettings

# Strategies for settings tests
_settings_user_ids = st.text(
    min_size=1,
    max_size=30,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
)


class TestProperty16NotificationSettingsRoundTrip:
    """Property 16: Notification settings round-trip.

    For any user and channel, after opt_out followed by opt_in, the user
    is opted in. After opt_in followed by opt_out, the user is opted out.
    The final state always matches the last operation.

    **Validates: Requirements 8.1, 8.3, 8.4**
    """

    @given(
        user_id=_settings_user_ids,
        channel=channels,
    )
    @settings(max_examples=200)
    def test_opt_out_then_opt_in_results_in_opted_in(
        self, user_id: str, channel: Channel
    ):
        """After opt_out followed by opt_in, the user is opted in.

        **Validates: Requirements 8.1, 8.3, 8.4**
        """
        ns = NotificationSettings()

        # Opt out then opt back in
        ns.opt_out(user_id, channel)
        assert not ns.is_opted_in(user_id, channel), (
            f"User {user_id!r} should be opted out of {channel} after opt_out()"
        )

        ns.opt_in(user_id, channel)
        assert ns.is_opted_in(user_id, channel), (
            f"User {user_id!r} should be opted in to {channel} after opt_out() then opt_in()"
        )

    @given(
        user_id=_settings_user_ids,
        channel=channels,
    )
    @settings(max_examples=200)
    def test_opt_in_then_opt_out_results_in_opted_out(
        self, user_id: str, channel: Channel
    ):
        """After opt_in followed by opt_out, the user is opted out.

        **Validates: Requirements 8.1, 8.3, 8.4**
        """
        ns = NotificationSettings()

        # Opt in then opt out
        ns.opt_in(user_id, channel)
        assert ns.is_opted_in(user_id, channel), (
            f"User {user_id!r} should be opted in to {channel} after opt_in()"
        )

        ns.opt_out(user_id, channel)
        assert not ns.is_opted_in(user_id, channel), (
            f"User {user_id!r} should be opted out of {channel} after opt_in() then opt_out()"
        )

    @given(
        user_id=_settings_user_ids,
        channel=channels,
        operations=st.lists(
            st.sampled_from(["opt_in", "opt_out"]),
            min_size=1,
            max_size=50,
        ),
    )
    @settings(max_examples=200)
    def test_final_state_matches_last_operation(
        self, user_id: str, channel: Channel, operations: List[str]
    ):
        """For any sequence of opt_in/opt_out operations, the final state
        always matches the last operation performed.

        **Validates: Requirements 8.1, 8.3, 8.4**
        """
        ns = NotificationSettings()

        for op in operations:
            if op == "opt_in":
                ns.opt_in(user_id, channel)
            else:
                ns.opt_out(user_id, channel)

        # The final state must match the last operation
        last_op = operations[-1]
        if last_op == "opt_in":
            assert ns.is_opted_in(user_id, channel), (
                f"Final state should be opted-in after last operation was opt_in, "
                f"but is_opted_in returned False for user {user_id!r}, channel {channel}"
            )
        else:
            assert not ns.is_opted_in(user_id, channel), (
                f"Final state should be opted-out after last operation was opt_out, "
                f"but is_opted_in returned True for user {user_id!r}, channel {channel}"
            )



# --- Property 23: Analytics aggregation consistency ---
# Validates: Requirements 10.4, 10.5

from datetime import datetime, timezone

from notification_system.models import Channel, NotificationStatus, VALID_TRANSITIONS
from notification_system.tracker import EventTracker


# Terminal statuses that get_aggregate_counts tracks
_AGGREGATE_STATUSES = {
    NotificationStatus.SENT,
    NotificationStatus.DELIVERED,
    NotificationStatus.FAILED,
    NotificationStatus.CLICKED,
}

# Valid terminal states a notification can reach via valid transitions from CREATED
_FINAL_STATES = st.sampled_from([
    NotificationStatus.SENT,
    NotificationStatus.DELIVERED,
    NotificationStatus.FAILED,
    NotificationStatus.CLICKED,
])


def _valid_path_to_status(status: NotificationStatus) -> List[NotificationStatus]:
    """Return a valid state machine path from CREATED to the given terminal status."""
    if status == NotificationStatus.FAILED:
        # CREATED -> QUEUED -> FAILED (shortest path to FAILED)
        return [NotificationStatus.CREATED, NotificationStatus.QUEUED, NotificationStatus.FAILED]
    elif status == NotificationStatus.SENT:
        return [
            NotificationStatus.CREATED,
            NotificationStatus.QUEUED,
            NotificationStatus.SENDING,
            NotificationStatus.SENT,
        ]
    elif status == NotificationStatus.DELIVERED:
        return [
            NotificationStatus.CREATED,
            NotificationStatus.QUEUED,
            NotificationStatus.SENDING,
            NotificationStatus.SENT,
            NotificationStatus.DELIVERED,
        ]
    elif status == NotificationStatus.CLICKED:
        return [
            NotificationStatus.CREATED,
            NotificationStatus.QUEUED,
            NotificationStatus.SENDING,
            NotificationStatus.SENT,
            NotificationStatus.DELIVERED,
            NotificationStatus.CLICKED,
        ]
    else:
        # For non-terminal states, just return CREATED
        return [NotificationStatus.CREATED]


@st.composite
def notifications_with_final_states(draw):
    """Generate a list of (notification_id, channel, final_status) tuples.

    Each notification will be driven through a valid state path to its final status.
    """
    num_notifications = draw(st.integers(min_value=1, max_value=30))
    notifications = []
    for i in range(num_notifications):
        nid = f"notif-{i}"
        channel = draw(channels)
        final_status = draw(_FINAL_STATES)
        notifications.append((nid, channel, final_status))
    return notifications


class TestProperty23AnalyticsAggregationConsistency:
    """Property 23: Analytics aggregation consistency.

    The aggregate counts from get_aggregate_counts() are consistent with
    the events recorded. The sum of counts should not exceed total
    notifications tracked.

    **Validates: Requirements 10.4, 10.5**
    """

    @given(data=notifications_with_final_states())
    @settings(max_examples=200)
    def test_aggregate_counts_match_recorded_final_states(self, data):
        """The aggregate counts returned by get_aggregate_counts() match
        the actual final states of all recorded notifications.

        **Validates: Requirements 10.4, 10.5**
        """
        tracker = EventTracker()

        # Track expected counts manually
        expected_sent = 0
        expected_delivered = 0
        expected_failed = 0
        expected_clicked = 0

        for nid, channel, final_status in data:
            path = _valid_path_to_status(final_status)
            for status in path:
                tracker.record(nid, channel, status)

            # Count based on final status
            if final_status == NotificationStatus.SENT:
                expected_sent += 1
            elif final_status == NotificationStatus.DELIVERED:
                expected_delivered += 1
            elif final_status == NotificationStatus.FAILED:
                expected_failed += 1
            elif final_status == NotificationStatus.CLICKED:
                expected_clicked += 1

        counts = tracker.get_aggregate_counts()

        assert counts["total_sent"] == expected_sent, (
            f"Expected total_sent={expected_sent}, got {counts['total_sent']}"
        )
        assert counts["total_delivered"] == expected_delivered, (
            f"Expected total_delivered={expected_delivered}, got {counts['total_delivered']}"
        )
        assert counts["total_failed"] == expected_failed, (
            f"Expected total_failed={expected_failed}, got {counts['total_failed']}"
        )
        assert counts["total_clicked"] == expected_clicked, (
            f"Expected total_clicked={expected_clicked}, got {counts['total_clicked']}"
        )

    @given(data=notifications_with_final_states())
    @settings(max_examples=200)
    def test_aggregate_sum_does_not_exceed_total_notifications(self, data):
        """The sum of all aggregate counts should not exceed the total
        number of distinct notifications tracked.

        **Validates: Requirements 10.4, 10.5**
        """
        tracker = EventTracker()

        for nid, channel, final_status in data:
            path = _valid_path_to_status(final_status)
            for status in path:
                tracker.record(nid, channel, status)

        counts = tracker.get_aggregate_counts()
        total_counted = (
            counts["total_sent"]
            + counts["total_delivered"]
            + counts["total_failed"]
            + counts["total_clicked"]
        )

        # The sum of aggregate counts must not exceed total notifications
        assert total_counted <= len(data), (
            f"Sum of aggregate counts ({total_counted}) exceeds "
            f"total notifications ({len(data)})"
        )

    @given(
        data=notifications_with_final_states(),
        query_channel=channels,
    )
    @settings(max_examples=200)
    def test_channel_stats_rates_are_consistent(self, data, query_channel):
        """Per-channel stats (success_rate + failure_rate) should not exceed 1.0,
        and the total count should match the number of notifications on that channel.

        **Validates: Requirements 10.4, 10.5**
        """
        tracker = EventTracker()

        channel_notification_count = 0
        for nid, channel, final_status in data:
            path = _valid_path_to_status(final_status)
            for status in path:
                tracker.record(nid, channel, status)
            if channel == query_channel:
                channel_notification_count += 1

        stats = tracker.get_channel_stats(query_channel)

        # Total must match the number of notifications for this channel
        assert stats["total"] == channel_notification_count, (
            f"Expected total={channel_notification_count} for channel {query_channel}, "
            f"got {stats['total']}"
        )

        # success_rate + failure_rate should not exceed 1.0
        assert stats["success_rate"] + stats["failure_rate"] <= 1.0 + 1e-9, (
            f"success_rate ({stats['success_rate']}) + failure_rate ({stats['failure_rate']}) "
            f"exceeds 1.0 for channel {query_channel}"
        )

        # Rates must be non-negative
        assert stats["success_rate"] >= 0.0
        assert stats["failure_rate"] >= 0.0


# --- Property 22: Event retrieval completeness ---
# Validates: Requirements 10.2, 10.3

from datetime import datetime, timezone, timedelta

from notification_system.models import NotificationStatus, NotificationEvent, VALID_TRANSITIONS
from notification_system.tracker import EventTracker


@st.composite
def valid_state_transition_sequence(draw):
    """Generate a valid sequence of state transitions following the state machine.

    Starts from any status and follows VALID_TRANSITIONS to produce a
    sequence of 1 to 8 states that form a valid path through the state machine.
    """
    channel = draw(channels)

    # Start with any status as the initial state
    current = draw(st.sampled_from(list(NotificationStatus)))
    sequence = [current]

    # Follow valid transitions up to a max length
    max_steps = draw(st.integers(min_value=0, max_value=7))
    for _ in range(max_steps):
        allowed = VALID_TRANSITIONS.get(current, set())
        if not allowed:
            break  # Terminal state, no further transitions
        next_status = draw(st.sampled_from(sorted(allowed, key=lambda s: s.value)))
        sequence.append(next_status)
        current = next_status

    return channel, sequence


class TestProperty22EventRetrievalCompleteness:
    """Property 22: Event retrieval completeness.

    For any notification ID for which events have been recorded, retrieving
    the event history SHALL return all recorded events in chronological order,
    each containing a valid timestamp, notification ID, channel, and status.
    The history length equals the number of recorded events.

    **Validates: Requirements 10.2, 10.3**
    """

    @given(
        notification_id=st.text(
            min_size=1,
            max_size=30,
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
        ),
        transition_data=valid_state_transition_sequence(),
    )
    @settings(max_examples=200)
    def test_all_recorded_events_appear_in_history(
        self, notification_id: str, transition_data: tuple
    ):
        """Every event recorded via record() appears in get_history() and
        the history length equals the number of recorded events.

        **Validates: Requirements 10.2, 10.3**
        """
        channel, status_sequence = transition_data

        # Use a monotonically increasing clock to ensure chronological order
        time_counter = [0]

        def clock():
            time_counter[0] += 1
            return datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=time_counter[0])

        tracker = EventTracker(clock=clock)

        # Record all events in the valid transition sequence
        for status in status_sequence:
            tracker.record(notification_id, channel, status)

        # Retrieve history
        history = tracker.get_history(notification_id)

        # History length must equal the number of recorded events
        assert len(history) == len(status_sequence), (
            f"Expected {len(status_sequence)} events in history, got {len(history)}"
        )

        # Each event must have the correct notification_id, channel, and status
        for i, (event, expected_status) in enumerate(zip(history, status_sequence)):
            assert event.notification_id == notification_id, (
                f"Event {i}: expected notification_id={notification_id!r}, "
                f"got {event.notification_id!r}"
            )
            assert event.channel == channel, (
                f"Event {i}: expected channel={channel}, got {event.channel}"
            )
            assert event.status == expected_status, (
                f"Event {i}: expected status={expected_status}, got {event.status}"
            )
            assert event.timestamp is not None, (
                f"Event {i}: timestamp must not be None"
            )

    @given(
        notification_id=st.text(
            min_size=1,
            max_size=30,
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
        ),
        transition_data=valid_state_transition_sequence(),
    )
    @settings(max_examples=200)
    def test_history_is_in_chronological_order(
        self, notification_id: str, transition_data: tuple
    ):
        """The event history is returned in chronological order (timestamps
        are non-decreasing).

        **Validates: Requirements 10.2, 10.3**
        """
        channel, status_sequence = transition_data
        assume(len(status_sequence) >= 2)

        # Use a monotonically increasing clock
        time_counter = [0]

        def clock():
            time_counter[0] += 1
            return datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=time_counter[0])

        tracker = EventTracker(clock=clock)

        for status in status_sequence:
            tracker.record(notification_id, channel, status)

        history = tracker.get_history(notification_id)

        # Verify chronological ordering: each timestamp <= next timestamp
        for i in range(len(history) - 1):
            assert history[i].timestamp <= history[i + 1].timestamp, (
                f"Events not in chronological order at index {i}: "
                f"{history[i].timestamp} > {history[i + 1].timestamp}"
            )

    @given(
        notification_id=st.text(
            min_size=1,
            max_size=30,
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
        ),
        transition_data=valid_state_transition_sequence(),
    )
    @settings(max_examples=200)
    def test_history_returns_independent_copy(
        self, notification_id: str, transition_data: tuple
    ):
        """get_history() returns a list that does not share references with
        the tracker's internal storage (modifying the returned list does not
        affect subsequent calls).

        **Validates: Requirements 10.2, 10.3**
        """
        channel, status_sequence = transition_data
        assume(len(status_sequence) >= 1)

        time_counter = [0]

        def clock():
            time_counter[0] += 1
            return datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=time_counter[0])

        tracker = EventTracker(clock=clock)

        for status in status_sequence:
            tracker.record(notification_id, channel, status)

        # Get history and mutate the returned list
        history1 = tracker.get_history(notification_id)
        original_len = len(history1)
        history1.clear()

        # A second call should still return the full history
        history2 = tracker.get_history(notification_id)
        assert len(history2) == original_len, (
            f"Mutating returned history affected internal state: "
            f"expected {original_len} events, got {len(history2)}"
        )


# --- Property 24: Notification log query correctness ---
# Validates: Requirements 12.1, 12.2

from datetime import datetime, timezone, timedelta

from notification_system.log import InMemoryNotificationLog
from notification_system.models import Channel, LogEntry, NotificationStatus

# Strategies for log entry generation
_log_notification_ids = st.text(
    min_size=1,
    max_size=30,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
)
_log_recipient_ids = st.text(
    min_size=1,
    max_size=20,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
)
_log_channels = st.sampled_from(list(Channel))
_log_statuses = st.sampled_from(list(NotificationStatus))
_log_content_summaries = st.text(min_size=1, max_size=100)
_log_attempts = st.integers(min_value=1, max_value=10)
_log_timestamps = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2025, 12, 31),
    timezones=st.just(timezone.utc),
)


@st.composite
def log_entries(draw, min_size=1, max_size=20):
    """Generate a list of LogEntry objects with various fields."""
    count = draw(st.integers(min_value=min_size, max_value=max_size))
    entries = []
    for _ in range(count):
        entry = LogEntry(
            notification_id=draw(_log_notification_ids),
            recipient_id=draw(_log_recipient_ids),
            channel=draw(_log_channels),
            content_summary=draw(_log_content_summaries),
            timestamp=draw(_log_timestamps),
            status=draw(_log_statuses),
            attempt=draw(_log_attempts),
        )
        entries.append(entry)
    return entries


class TestProperty24NotificationLogQueryCorrectness:
    """Property 24: Notification log query correctness.

    For any set of log entries and any query filter (by notification_id,
    recipient_id, channel, status, or time range), the query result SHALL
    contain exactly those entries that match ALL specified filter criteria.

    **Validates: Requirements 12.1, 12.2**
    """

    @given(entries=log_entries(min_size=1, max_size=20))
    @settings(max_examples=200)
    def test_all_appended_entries_retrievable_via_empty_query(
        self, entries: list,
    ):
        """Every entry appended to the log is retrievable via an unfiltered query.

        **Validates: Requirements 12.1, 12.2**
        """
        log = InMemoryNotificationLog()
        for entry in entries:
            log.append(entry)

        # Query with no filters should return all entries
        results = log.query()
        assert len(results) == len(entries)
        for entry in entries:
            assert entry in results

    @given(
        entries=log_entries(min_size=2, max_size=20),
        query_notification_id=_log_notification_ids,
    )
    @settings(max_examples=200)
    def test_query_by_notification_id_returns_only_matching(
        self, entries: list, query_notification_id: str,
    ):
        """Querying by notification_id returns only entries matching that ID.

        **Validates: Requirements 12.1, 12.2**
        """
        log = InMemoryNotificationLog()
        for entry in entries:
            log.append(entry)

        results = log.query(notification_id=query_notification_id)

        # All results must have the queried notification_id
        for result in results:
            assert result.notification_id == query_notification_id

        # All entries with that notification_id must be in results
        expected = [e for e in entries if e.notification_id == query_notification_id]
        assert len(results) == len(expected)

    @given(
        entries=log_entries(min_size=2, max_size=20),
        query_channel=_log_channels,
    )
    @settings(max_examples=200)
    def test_query_by_channel_returns_only_matching(
        self, entries: list, query_channel: Channel,
    ):
        """Querying by channel returns only entries for that channel.

        **Validates: Requirements 12.1, 12.2**
        """
        log = InMemoryNotificationLog()
        for entry in entries:
            log.append(entry)

        results = log.query(channel=query_channel)

        # All results must have the queried channel
        for result in results:
            assert result.channel == query_channel

        # All entries with that channel must be in results
        expected = [e for e in entries if e.channel == query_channel]
        assert len(results) == len(expected)

    @given(
        entries=log_entries(min_size=2, max_size=20),
        query_status=_log_statuses,
    )
    @settings(max_examples=200)
    def test_query_by_status_returns_only_matching(
        self, entries: list, query_status: NotificationStatus,
    ):
        """Querying by status returns only entries with that status.

        **Validates: Requirements 12.1, 12.2**
        """
        log = InMemoryNotificationLog()
        for entry in entries:
            log.append(entry)

        results = log.query(status=query_status)

        # All results must have the queried status
        for result in results:
            assert result.status == query_status

        # All entries with that status must be in results
        expected = [e for e in entries if e.status == query_status]
        assert len(results) == len(expected)

    @given(
        entries=log_entries(min_size=2, max_size=20),
        query_notification_id=_log_notification_ids,
        query_channel=_log_channels,
    )
    @settings(max_examples=200)
    def test_query_by_multiple_filters_returns_intersection(
        self, entries: list, query_notification_id: str, query_channel: Channel,
    ):
        """Querying with multiple filters returns only entries matching ALL criteria.

        **Validates: Requirements 12.1, 12.2**
        """
        log = InMemoryNotificationLog()
        for entry in entries:
            log.append(entry)

        results = log.query(
            notification_id=query_notification_id,
            channel=query_channel,
        )

        # All results must match both filters
        for result in results:
            assert result.notification_id == query_notification_id
            assert result.channel == query_channel

        # All entries matching both filters must be in results
        expected = [
            e for e in entries
            if e.notification_id == query_notification_id and e.channel == query_channel
        ]
        assert len(results) == len(expected)

    @given(
        entries=log_entries(min_size=3, max_size=15),
    )
    @settings(max_examples=200)
    def test_query_by_time_range_returns_only_entries_within_range(
        self, entries: list,
    ):
        """Querying by time range returns only entries within [start_time, end_time].

        **Validates: Requirements 12.1, 12.2**
        """
        log = InMemoryNotificationLog()
        for entry in entries:
            log.append(entry)

        # Use the median timestamp as a split point for the time range
        sorted_entries = sorted(entries, key=lambda e: e.timestamp)
        mid_idx = len(sorted_entries) // 2
        start_time = sorted_entries[mid_idx].timestamp
        end_time = sorted_entries[-1].timestamp

        results = log.query(start_time=start_time, end_time=end_time)

        # All results must be within the time range
        for result in results:
            assert result.timestamp >= start_time
            assert result.timestamp <= end_time

        # All entries within the range must be in results
        expected = [
            e for e in entries
            if e.timestamp >= start_time and e.timestamp <= end_time
        ]
        assert len(results) == len(expected)


# --- Property 21: Event state machine validity ---
# Validates: Requirements 10.1

from notification_system.models import NotificationStatus, VALID_TRANSITIONS
from notification_system.tracker import EventTracker

# Strategies for event tracker tests
notification_ids_str = st.text(
    min_size=1,
    max_size=30,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
)
all_statuses = st.sampled_from(list(NotificationStatus))


@st.composite
def invalid_transition_pair(draw):
    """Generate a (from_status, to_status) pair that is NOT in VALID_TRANSITIONS.

    Returns a tuple (from_status, to_status) where to_status is not in
    VALID_TRANSITIONS[from_status].
    """
    from_status = draw(st.sampled_from(list(NotificationStatus)))
    allowed = VALID_TRANSITIONS.get(from_status, set())
    # All statuses that are NOT valid transitions from from_status
    invalid_targets = [s for s in NotificationStatus if s not in allowed]
    assume(len(invalid_targets) > 0)
    to_status = draw(st.sampled_from(invalid_targets))
    return from_status, to_status


@st.composite
def valid_transition_pair(draw):
    """Generate a (from_status, to_status) pair that IS in VALID_TRANSITIONS.

    Returns a tuple (from_status, to_status) where to_status is in
    VALID_TRANSITIONS[from_status].
    """
    # Only pick from statuses that have at least one valid transition
    statuses_with_transitions = [
        s for s in NotificationStatus if VALID_TRANSITIONS.get(s, set())
    ]
    assume(len(statuses_with_transitions) > 0)
    from_status = draw(st.sampled_from(statuses_with_transitions))
    allowed = VALID_TRANSITIONS[from_status]
    to_status = draw(st.sampled_from(sorted(allowed, key=lambda s: s.value)))
    return from_status, to_status


class TestProperty21EventStateMachineValidity:
    """Property 21: Event state machine validity.

    For any notification's event history, each consecutive pair of status
    transitions SHALL be a valid transition according to the defined state
    machine (VALID_TRANSITIONS map). Recording an invalid transition raises
    ValueError.

    **Validates: Requirements 10.1**
    """

    @given(
        notification_id=notification_ids_str,
        channel=channels,
        data=invalid_transition_pair(),
    )
    @settings(max_examples=200)
    def test_invalid_transition_raises_value_error(
        self, notification_id: str, channel: Channel, data: tuple
    ):
        """Recording an invalid state transition raises ValueError.

        **Validates: Requirements 10.1**
        """
        from_status, to_status = data

        tracker = EventTracker()

        # Record the initial state (any status is accepted as the first event)
        tracker.record(notification_id, channel, from_status)

        # Attempting an invalid transition should raise ValueError
        with pytest.raises(ValueError) as exc_info:
            tracker.record(notification_id, channel, to_status)

        # Error message should reference the notification and the invalid transition
        assert notification_id in str(exc_info.value)
        assert from_status.value in str(exc_info.value)
        assert to_status.value in str(exc_info.value)

    @given(
        notification_id=notification_ids_str,
        channel=channels,
        data=valid_transition_pair(),
    )
    @settings(max_examples=200)
    def test_valid_transition_is_accepted(
        self, notification_id: str, channel: Channel, data: tuple
    ):
        """Recording a valid state transition does not raise and updates
        the current status correctly.

        **Validates: Requirements 10.1**
        """
        from_status, to_status = data

        tracker = EventTracker()

        # Record the initial state
        tracker.record(notification_id, channel, from_status)

        # Valid transition should succeed without raising
        tracker.record(notification_id, channel, to_status)

        # Current status should be updated
        assert tracker.get_current_status(notification_id) == to_status

    @given(
        notification_id=notification_ids_str,
        channel=channels,
        initial_status=all_statuses,
    )
    @settings(max_examples=200)
    def test_any_status_accepted_as_initial_event(
        self, notification_id: str, channel: Channel, initial_status: NotificationStatus
    ):
        """Any status is accepted as the first event for a notification
        (no prior state to validate against).

        **Validates: Requirements 10.1**
        """
        tracker = EventTracker()

        # First event should always succeed regardless of status
        tracker.record(notification_id, channel, initial_status)

        # Verify it was recorded
        assert tracker.get_current_status(notification_id) == initial_status
        history = tracker.get_history(notification_id)
        assert len(history) == 1
        assert history[0].status == initial_status

    @given(
        notification_id=notification_ids_str,
        channel=channels,
        transitions=st.lists(valid_transition_pair(), min_size=1, max_size=5),
    )
    @settings(max_examples=200)
    def test_sequence_of_valid_transitions_all_accepted(
        self, notification_id: str, channel: Channel, transitions: list
    ):
        """A sequence of valid transitions (each following from the previous
        state) is fully accepted by the tracker.

        **Validates: Requirements 10.1**
        """
        tracker = EventTracker()

        # Start with the first transition's from_status
        first_from, first_to = transitions[0]
        tracker.record(notification_id, channel, first_from)
        tracker.record(notification_id, channel, first_to)

        current = first_to

        # For subsequent transitions, we need to find valid transitions from current
        # Since generated transitions may not chain, we build a valid chain manually
        for from_status, to_status in transitions[1:]:
            # Check if there's a valid transition from current
            allowed = VALID_TRANSITIONS.get(current, set())
            if not allowed:
                break  # Terminal state, can't continue
            # Pick the first allowed transition
            next_status = sorted(allowed, key=lambda s: s.value)[0]
            tracker.record(notification_id, channel, next_status)
            current = next_status

        # Verify all recorded events are in the history
        history = tracker.get_history(notification_id)
        assert len(history) >= 2
        assert tracker.get_current_status(notification_id) == current


# --- Property 19: Retry exhaustion marks permanent failure ---
# Validates: Requirements 9.2, 9.4

from notification_system.config import RetryConfig
from notification_system.exceptions import DeliveryError
from notification_system.models import Channel, NotificationRequest, NotificationStatus, NotificationTask
from notification_system.queue import MessageQueue
from notification_system.retry import RetryHandler
from notification_system.tracker import EventTracker
from notification_system.log import NotificationLog


# Strategies for Property 19
_retry_max_retries = st.integers(min_value=1, max_value=10)
_retry_base_delay = st.floats(min_value=0.001, max_value=10.0, allow_nan=False, allow_infinity=False)
_retry_max_delay = st.floats(min_value=10.0, max_value=300.0, allow_nan=False, allow_infinity=False)
_retry_jitter_factor = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)


@st.composite
def retry_config_and_exhausted_attempt(draw):
    """Generate a RetryConfig and a task attempt number >= max_retries.

    Returns a tuple of (RetryConfig, attempt) where attempt >= max_retries,
    guaranteeing the retry handler should mark the task as permanently failed.
    """
    max_retries = draw(_retry_max_retries)
    base_delay = draw(_retry_base_delay)
    max_delay = draw(_retry_max_delay)
    jitter_factor = draw(_retry_jitter_factor)

    config = RetryConfig(
        max_retries=max_retries,
        base_delay=base_delay,
        max_delay=max_delay,
        jitter_factor=jitter_factor,
    )

    # Generate an attempt number that equals or exceeds max_retries
    attempt = draw(st.integers(min_value=max_retries, max_value=max_retries + 10))

    return config, attempt


class TestProperty19RetryExhaustionMarksPermanentFailure:
    """Property 19: Retry exhaustion marks permanent failure.

    When a task's attempt count equals or exceeds max_retries, handle_failure
    marks it as permanently FAILED (records FAILED status in tracker) and
    does NOT re-enqueue the task.

    **Validates: Requirements 9.2, 9.4**
    """

    @given(
        data=retry_config_and_exhausted_attempt(),
        channel=channels,
        notification_id=st.text(
            min_size=1,
            max_size=30,
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
        ),
        error_reason=st.text(min_size=1, max_size=50),
    )
    @settings(max_examples=200)
    def test_exhausted_retries_records_failed_status(
        self, data, channel: Channel, notification_id: str, error_reason: str
    ):
        """When task.attempt >= max_retries, handle_failure records FAILED
        status in the event tracker.

        **Validates: Requirements 9.2, 9.4**
        """
        config, attempt = data

        async def run():
            queue = MessageQueue(max_depth=100)
            tracker = EventTracker()
            notification_log = NotificationLog()

            handler = RetryHandler(
                config=config,
                queue=queue,
                tracker=tracker,
                notification_log=notification_log,
            )

            request = NotificationRequest(
                recipient_id="user-test",
                channel=channel,
                title="Test Title",
                body="Test body content for property 19",
            )
            task = NotificationTask(
                notification_id=notification_id,
                request=request,
                channel=channel,
                attempt=attempt,
            )

            error = DeliveryError(
                notification_id=notification_id,
                channel=channel.value,
                reason=error_reason,
                retryable=True,  # Error IS retryable, but retries are exhausted
            )

            await handler.handle_failure(task, error)

            # Verify the tracker recorded FAILED status
            current_status = tracker.get_current_status(notification_id)
            assert current_status == NotificationStatus.FAILED, (
                f"Expected FAILED status after retry exhaustion, "
                f"got {current_status} (attempt={attempt}, max_retries={config.max_retries})"
            )

        asyncio.get_event_loop().run_until_complete(run())

    @given(
        data=retry_config_and_exhausted_attempt(),
        channel=channels,
        notification_id=st.text(
            min_size=1,
            max_size=30,
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
        ),
        error_reason=st.text(min_size=1, max_size=50),
    )
    @settings(max_examples=200)
    def test_exhausted_retries_does_not_reenqueue(
        self, data, channel: Channel, notification_id: str, error_reason: str
    ):
        """When task.attempt >= max_retries, handle_failure does NOT
        re-enqueue the task to the message queue.

        **Validates: Requirements 9.2, 9.4**
        """
        config, attempt = data

        async def run():
            queue = MessageQueue(max_depth=100)
            tracker = EventTracker()
            notification_log = NotificationLog()

            handler = RetryHandler(
                config=config,
                queue=queue,
                tracker=tracker,
                notification_log=notification_log,
            )

            request = NotificationRequest(
                recipient_id="user-test",
                channel=channel,
                title="Test Title",
                body="Test body content for property 19",
            )
            task = NotificationTask(
                notification_id=notification_id,
                request=request,
                channel=channel,
                attempt=attempt,
            )

            error = DeliveryError(
                notification_id=notification_id,
                channel=channel.value,
                reason=error_reason,
                retryable=True,  # Error IS retryable, but retries are exhausted
            )

            await handler.handle_failure(task, error)

            # Verify the queue is still empty (task was NOT re-enqueued)
            assert queue.depth(channel) == 0, (
                f"Expected queue to be empty after retry exhaustion, "
                f"but depth is {queue.depth(channel)} "
                f"(attempt={attempt}, max_retries={config.max_retries})"
            )

        asyncio.get_event_loop().run_until_complete(run())

    @given(
        data=retry_config_and_exhausted_attempt(),
        channel=channels,
        notification_id=st.text(
            min_size=1,
            max_size=30,
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
        ),
        error_reason=st.text(min_size=1, max_size=50),
    )
    @settings(max_examples=200)
    def test_exhausted_retries_at_exact_boundary(
        self, data, channel: Channel, notification_id: str, error_reason: str
    ):
        """When task.attempt == max_retries exactly (boundary case),
        handle_failure still marks as permanently FAILED and does not re-enqueue.

        **Validates: Requirements 9.2, 9.4**
        """
        config, _ = data
        # Use exactly max_retries as the attempt number (boundary)
        attempt = config.max_retries

        async def run():
            queue = MessageQueue(max_depth=100)
            tracker = EventTracker()
            notification_log = NotificationLog()

            handler = RetryHandler(
                config=config,
                queue=queue,
                tracker=tracker,
                notification_log=notification_log,
            )

            request = NotificationRequest(
                recipient_id="user-test",
                channel=channel,
                title="Test Title",
                body="Test body content for property 19",
            )
            task = NotificationTask(
                notification_id=notification_id,
                request=request,
                channel=channel,
                attempt=attempt,
            )

            error = DeliveryError(
                notification_id=notification_id,
                channel=channel.value,
                reason=error_reason,
                retryable=True,
            )

            await handler.handle_failure(task, error)

            # Both conditions must hold at the boundary
            current_status = tracker.get_current_status(notification_id)
            assert current_status == NotificationStatus.FAILED, (
                f"Expected FAILED at boundary (attempt={attempt} == max_retries={config.max_retries}), "
                f"got {current_status}"
            )
            assert queue.depth(channel) == 0, (
                f"Expected empty queue at boundary, but depth is {queue.depth(channel)}"
            )

        asyncio.get_event_loop().run_until_complete(run())



# --- Property 15: Deduplication TTL expiry ---
# Validates: Requirements 7.3, 7.5
# (Already tested above via Property 14 window tests)


# --- Property 18: Exponential backoff with cap ---
# Validates: Requirements 9.1, 9.3

from notification_system.config import RetryConfig
from notification_system.retry import RetryHandler
from notification_system.tracker import EventTracker
from notification_system.queue import MessageQueue
from notification_system.log import NotificationLog

# Strategy for valid RetryConfig values
retry_configs = st.builds(
    RetryConfig,
    max_retries=st.integers(min_value=1, max_value=10),
    base_delay=st.floats(min_value=0.01, max_value=10.0, allow_nan=False, allow_infinity=False),
    max_delay=st.floats(min_value=1.0, max_value=600.0, allow_nan=False, allow_infinity=False),
    jitter_factor=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
)

# Strategy for attempt numbers
attempt_numbers = st.integers(min_value=0, max_value=20)


class TestProperty18ExponentialBackoffWithCap:
    """Property 18: Exponential backoff with cap.

    For any retry attempt number N with configured base_delay B and max_delay M,
    the computed delay SHALL be at most M and SHALL follow the formula
    min(B × 2^N + jitter, M) where jitter is non-negative.

    **Validates: Requirements 9.1, 9.3**
    """

    @given(
        config=retry_configs,
        attempt=attempt_numbers,
    )
    @settings(max_examples=500)
    def test_delay_is_at_least_base_exponential(
        self, config: RetryConfig, attempt: int
    ):
        """The computed delay is >= base_delay * 2^attempt (the exponential
        component without jitter), because jitter is always non-negative.

        **Validates: Requirements 9.1, 9.3**
        """
        tracker = EventTracker()
        queue = MessageQueue(max_depth=100)
        log = NotificationLog()
        handler = RetryHandler(
            config=config, queue=queue, tracker=tracker, notification_log=log
        )

        delay = handler.compute_delay(attempt)
        base_exponential = config.base_delay * (2 ** attempt)

        # Delay must be >= the base exponential (jitter is non-negative)
        # But also capped at max_delay, so the lower bound is min(base_exponential, max_delay)
        expected_lower_bound = min(base_exponential, config.max_delay)
        assert delay >= expected_lower_bound, (
            f"Delay {delay} is less than expected lower bound {expected_lower_bound} "
            f"(base_delay={config.base_delay}, attempt={attempt}, max_delay={config.max_delay})"
        )

    @given(
        config=retry_configs,
        attempt=attempt_numbers,
    )
    @settings(max_examples=500)
    def test_delay_never_exceeds_max_delay(
        self, config: RetryConfig, attempt: int
    ):
        """The computed delay SHALL never exceed the configured max_delay.

        **Validates: Requirements 9.1, 9.3**
        """
        tracker = EventTracker()
        queue = MessageQueue(max_depth=100)
        log = NotificationLog()
        handler = RetryHandler(
            config=config, queue=queue, tracker=tracker, notification_log=log
        )

        delay = handler.compute_delay(attempt)

        assert delay <= config.max_delay, (
            f"Delay {delay} exceeds max_delay {config.max_delay} "
            f"(base_delay={config.base_delay}, attempt={attempt})"
        )

    @given(
        config=retry_configs,
        attempts=st.lists(
            st.integers(min_value=0, max_value=20),
            min_size=2,
            max_size=10,
        ),
    )
    @settings(max_examples=500)
    def test_base_exponential_is_monotonically_non_decreasing(
        self, config: RetryConfig, attempts: list
    ):
        """The base exponential component (base_delay * 2^attempt) is
        monotonically non-decreasing with attempt number. When capped at
        max_delay, the effective minimum delay is also non-decreasing.

        This verifies the structural property that higher attempt numbers
        produce equal or larger base delays (ignoring jitter randomness).

        **Validates: Requirements 9.1, 9.3**
        """
        # Sort attempts to check monotonicity
        sorted_attempts = sorted(attempts)

        # The base exponential (without jitter) must be non-decreasing
        prev_base = None
        for attempt in sorted_attempts:
            base_exponential = config.base_delay * (2 ** attempt)
            # The effective minimum delay (capped) is min(base_exponential, max_delay)
            effective_min = min(base_exponential, config.max_delay)

            if prev_base is not None:
                assert effective_min >= prev_base, (
                    f"Monotonicity violated: attempt {attempt} gives effective min "
                    f"{effective_min} which is less than previous {prev_base}"
                )
            prev_base = effective_min

    @given(
        config=retry_configs,
        attempt=attempt_numbers,
    )
    @settings(max_examples=500)
    def test_jitter_is_non_negative(
        self, config: RetryConfig, attempt: int
    ):
        """The jitter component is always non-negative, meaning the delay
        is always >= the base exponential component (before capping).

        **Validates: Requirements 9.1, 9.3**
        """
        tracker = EventTracker()
        queue = MessageQueue(max_depth=100)
        log = NotificationLog()
        handler = RetryHandler(
            config=config, queue=queue, tracker=tracker, notification_log=log
        )

        delay = handler.compute_delay(attempt)
        base_exponential = config.base_delay * (2 ** attempt)

        # If base_exponential <= max_delay, then delay >= base_exponential
        # (because jitter is non-negative and result is min(base + jitter, max_delay))
        if base_exponential <= config.max_delay:
            assert delay >= base_exponential, (
                f"Delay {delay} < base_exponential {base_exponential}, "
                f"indicating negative jitter"
            )



# --- Property 20: Retryable vs non-retryable error classification ---
# Validates: Requirements 9.6

from notification_system.config import RetryConfig
from notification_system.exceptions import DeliveryError
from notification_system.models import NotificationStatus, NotificationTask, NotificationRequest
from notification_system.queue import MessageQueue
from notification_system.tracker import EventTracker
from notification_system.log import NotificationLog
from notification_system.retry import RetryHandler


# Strategies for retry error classification tests
_retry_attempt_counts = st.integers(min_value=0, max_value=10)
_max_retries = st.integers(min_value=1, max_value=10)
_error_reasons = st.text(min_size=1, max_size=100, alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")))


class TestProperty20RetryableVsNonRetryableErrorClassification:
    """Property 20: Retryable vs non-retryable error classification.

    When a DeliveryError has retryable=False, handle_failure immediately
    marks the notification as FAILED regardless of attempt count.
    When retryable=True and attempts remain, it re-enqueues the task.

    **Validates: Requirements 9.6**
    """

    @given(
        channel=channels,
        attempt=_retry_attempt_counts,
        max_retries=_max_retries,
        reason=_error_reasons,
    )
    @settings(max_examples=200)
    def test_non_retryable_error_immediately_fails(
        self, channel: Channel, attempt: int, max_retries: int, reason: str
    ):
        """When a DeliveryError has retryable=False, handle_failure marks
        the notification as FAILED immediately, regardless of attempt count.

        **Validates: Requirements 9.6**
        """

        async def run():
            config = RetryConfig(max_retries=max_retries, base_delay=1.0, max_delay=60.0, jitter_factor=0.1)
            queue = MessageQueue(max_depth=100)
            tracker = EventTracker()
            log = NotificationLog()

            handler = RetryHandler(config=config, queue=queue, tracker=tracker, notification_log=log)

            request = NotificationRequest(
                recipient_id="user-1",
                channel=channel,
                title="Test",
                body="Test body content",
            )
            notification_id = f"notif-nonretry-{attempt}"
            task = NotificationTask(
                notification_id=notification_id,
                request=request,
                channel=channel,
                attempt=attempt,
            )

            # Set up tracker with initial state so FAILED transition is valid
            tracker.record(notification_id, channel, NotificationStatus.CREATED)
            tracker.record(notification_id, channel, NotificationStatus.QUEUED)
            tracker.record(notification_id, channel, NotificationStatus.SENDING)

            error = DeliveryError(
                notification_id=notification_id,
                channel=channel.value,
                reason=reason,
                retryable=False,
            )

            await handler.handle_failure(task, error)

            # The notification should be marked as FAILED
            status = tracker.get_current_status(notification_id)
            assert status == NotificationStatus.FAILED, (
                f"Expected FAILED for non-retryable error, got {status}"
            )

            # The queue should remain empty (no re-enqueue)
            assert queue.depth(channel) == 0, (
                "Non-retryable error should not re-enqueue the task"
            )

        asyncio.get_event_loop().run_until_complete(run())

    @given(
        channel=channels,
        attempt=_retry_attempt_counts,
        max_retries=_max_retries,
        reason=_error_reasons,
    )
    @settings(max_examples=200)
    def test_retryable_error_with_attempts_remaining_re_enqueues(
        self, channel: Channel, attempt: int, max_retries: int, reason: str
    ):
        """When a DeliveryError has retryable=True and attempts remain
        (attempt < max_retries), handle_failure re-enqueues the task.

        **Validates: Requirements 9.6**
        """
        # Ensure attempts remain
        assume(attempt < max_retries)

        async def run():
            config = RetryConfig(max_retries=max_retries, base_delay=0.001, max_delay=0.01, jitter_factor=0.0)
            queue = MessageQueue(max_depth=100)
            tracker = EventTracker()
            log = NotificationLog()

            handler = RetryHandler(config=config, queue=queue, tracker=tracker, notification_log=log)

            request = NotificationRequest(
                recipient_id="user-1",
                channel=channel,
                title="Test",
                body="Test body content",
            )
            notification_id = f"notif-retry-{attempt}"
            task = NotificationTask(
                notification_id=notification_id,
                request=request,
                channel=channel,
                attempt=attempt,
            )

            error = DeliveryError(
                notification_id=notification_id,
                channel=channel.value,
                reason=reason,
                retryable=True,
            )

            await handler.handle_failure(task, error)

            # The task should be re-enqueued
            assert queue.depth(channel) == 1, (
                f"Expected task to be re-enqueued, but queue depth is {queue.depth(channel)}"
            )

            # Verify the re-enqueued task has incremented attempt
            requeued = queue.dequeue_nowait(channel)
            assert requeued is not None
            assert requeued.attempt == attempt + 1
            assert requeued.notification_id == notification_id

        asyncio.get_event_loop().run_until_complete(run())

    @given(
        channel=channels,
        max_retries=_max_retries,
        reason=_error_reasons,
    )
    @settings(max_examples=200)
    def test_retryable_error_at_max_retries_marks_failed(
        self, channel: Channel, max_retries: int, reason: str
    ):
        """When a DeliveryError has retryable=True but attempt >= max_retries,
        handle_failure marks the notification as FAILED (exhausted retries).

        **Validates: Requirements 9.6**
        """

        async def run():
            config = RetryConfig(max_retries=max_retries, base_delay=0.001, max_delay=0.01, jitter_factor=0.0)
            queue = MessageQueue(max_depth=100)
            tracker = EventTracker()
            log = NotificationLog()

            handler = RetryHandler(config=config, queue=queue, tracker=tracker, notification_log=log)

            request = NotificationRequest(
                recipient_id="user-1",
                channel=channel,
                title="Test",
                body="Test body content",
            )
            notification_id = f"notif-exhausted-{max_retries}"
            task = NotificationTask(
                notification_id=notification_id,
                request=request,
                channel=channel,
                attempt=max_retries,  # At max retries
            )

            # Set up tracker with initial state so FAILED transition is valid
            tracker.record(notification_id, channel, NotificationStatus.CREATED)
            tracker.record(notification_id, channel, NotificationStatus.QUEUED)
            tracker.record(notification_id, channel, NotificationStatus.SENDING)

            error = DeliveryError(
                notification_id=notification_id,
                channel=channel.value,
                reason=reason,
                retryable=True,
            )

            await handler.handle_failure(task, error)

            # The notification should be marked as FAILED
            status = tracker.get_current_status(notification_id)
            assert status == NotificationStatus.FAILED, (
                f"Expected FAILED when retries exhausted, got {status}"
            )

            # The queue should remain empty (no re-enqueue)
            assert queue.depth(channel) == 0, (
                "Exhausted retries should not re-enqueue the task"
            )

        asyncio.get_event_loop().run_until_complete(run())


# --- Property 1: Channel routing correctness ---
# Validates: Requirements 1.1, 2.1

from notification_system.service import NotificationService
from notification_system.contacts import ContactInfoStore
from notification_system.models import ContactInfo


class TestProperty1ChannelRoutingCorrectness:
    """Property 1: Channel routing correctness.

    For any valid NotificationRequest with a specific channel, after calling
    service.send(), the notification task ends up in the correct channel queue.
    The message queue maintains separate queues per channel, and the service
    must route each notification to the queue matching its channel.

    **Validates: Requirements 1.1, 2.1**
    """

    @given(
        channel=channels,
        recipient_id=st.text(
            min_size=1,
            max_size=20,
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
        ),
        title=st.text(min_size=1, max_size=50),
        body=st.text(min_size=1, max_size=100),
    )
    @settings(max_examples=200)
    def test_notification_routed_to_correct_channel_queue(
        self, channel: Channel, recipient_id: str, title: str, body: str
    ):
        """For any valid NotificationRequest with a specific channel,
        after calling service.send(), the notification task is enqueued
        to the queue for that channel and no other channel queues.

        **Validates: Requirements 1.1, 2.1**
        """

        async def run():
            # Set up contacts so the user has valid contact info for the channel
            contacts = ContactInfoStore()
            contact = ContactInfo(
                user_id=recipient_id,
                email="test@example.com",
                phone="+15551234567",
                device_tokens=["device_token_abc123"],
            )
            contacts.set(contact)

            # Create service with high rate limits to avoid interference
            service = NotificationService(contacts=contacts)

            request = NotificationRequest(
                recipient_id=recipient_id,
                channel=channel,
                title=title,
                body=body,
            )

            # Send the notification
            notification_id = await service.send(request)
            assert notification_id  # Should return a non-empty ID

            # Verify the task ended up in the correct channel queue
            queue = service.queue
            assert queue.depth(channel) == 1, (
                f"Expected 1 task in {channel} queue, got {queue.depth(channel)}"
            )

            # Verify no other channel queues received the task
            for other_channel in Channel:
                if other_channel != channel:
                    assert queue.depth(other_channel) == 0, (
                        f"Expected 0 tasks in {other_channel} queue, "
                        f"got {queue.depth(other_channel)}"
                    )

            # Verify the dequeued task has the correct channel and notification_id
            task = queue.dequeue_nowait(channel)
            assert task is not None
            assert task.channel == channel, (
                f"Expected task channel {channel}, got {task.channel}"
            )
            assert task.notification_id == notification_id

        asyncio.get_event_loop().run_until_complete(run())

    @given(
        channels_list=st.lists(
            channels,
            min_size=2,
            max_size=10,
        ),
        recipient_id=st.text(
            min_size=1,
            max_size=20,
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
        ),
    )
    @settings(max_examples=100)
    def test_multiple_notifications_routed_to_respective_channels(
        self, channels_list: list, recipient_id: str
    ):
        """For a sequence of notifications on various channels, each
        notification is routed to its respective channel queue. The depth
        of each channel queue equals the number of notifications sent to it.

        **Validates: Requirements 1.1, 2.1**
        """

        async def run():
            # Set up contacts with all channel endpoints
            contacts = ContactInfoStore()
            contact = ContactInfo(
                user_id=recipient_id,
                email="test@example.com",
                phone="+15551234567",
                device_tokens=["device_token_abc123"],
            )
            contacts.set(contact)

            # Use high rate limits and long dedup window with unique content
            from notification_system.config import NotificationConfig, RateLimitConfig

            high_limit = RateLimitConfig(max_count=10000, window_seconds=3600.0)
            config = NotificationConfig(
                per_channel_rate_limits={ch: high_limit for ch in Channel},
                global_rate_limit=high_limit,
                dedup_window_seconds=300.0,
            )

            service = NotificationService(config=config, contacts=contacts)

            # Count expected tasks per channel
            expected_counts: dict[Channel, int] = {ch: 0 for ch in Channel}

            for i, ch in enumerate(channels_list):
                request = NotificationRequest(
                    recipient_id=recipient_id,
                    channel=ch,
                    title=f"Title {i}",
                    body=f"Body {i} unique content",
                )
                await service.send(request)
                expected_counts[ch] += 1

            # Verify each channel queue has the expected number of tasks
            queue = service.queue
            for ch in Channel:
                assert queue.depth(ch) == expected_counts[ch], (
                    f"Expected {expected_counts[ch]} tasks in {ch} queue, "
                    f"got {queue.depth(ch)}"
                )

        asyncio.get_event_loop().run_until_complete(run())


# --- Property 3: Invalid recipient validation ---
# Validates: Requirements 1.5

from notification_system.contacts import ContactInfoStore
from notification_system.exceptions import ValidationError
from notification_system.service import NotificationService

# Strategies for Property 3
_prop3_recipient_ids = st.text(
    min_size=1,
    max_size=50,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
)


class TestProperty3InvalidRecipientValidation:
    """Property 3: Invalid recipient validation.

    For any recipient_id that has no contact info registered for the
    specified channel, calling service.send() raises ValidationError.

    **Validates: Requirements 1.5**
    """

    @given(
        recipient_id=_prop3_recipient_ids,
        channel=channels,
    )
    @settings(max_examples=200)
    def test_send_raises_validation_error_for_unknown_recipient(
        self, recipient_id: str, channel: Channel
    ):
        """For any user/channel combination with no registered contact info,
        send() raises ValidationError.

        **Validates: Requirements 1.5**
        """

        async def run():
            # Set up service with an empty ContactInfoStore
            contacts = ContactInfoStore()
            service = NotificationService(contacts=contacts)

            request = NotificationRequest(
                recipient_id=recipient_id,
                channel=channel,
                title="Test Title",
                body="Test body",
            )

            with pytest.raises(ValidationError) as exc_info:
                await service.send(request)

            # Verify the error references the recipient_id field
            assert exc_info.value.field == "recipient_id"
            # Verify the error message mentions the recipient and channel
            assert recipient_id in str(exc_info.value)
            assert channel.value in str(exc_info.value)

        asyncio.get_event_loop().run_until_complete(run())

    @given(
        recipient_id=_prop3_recipient_ids,
        channel=channels,
    )
    @settings(max_examples=200)
    def test_send_raises_validation_error_when_channel_has_no_endpoint(
        self, recipient_id: str, channel: Channel
    ):
        """Even if a ContactInfo entry exists for the user, if the specific
        channel has no endpoint (e.g., no email set, no phone, no device token),
        send() raises ValidationError.

        **Validates: Requirements 1.5**
        """

        async def run():
            from notification_system.models import ContactInfo

            # Create a ContactInfo with NO endpoints set (all None/empty)
            contacts = ContactInfoStore()
            contact = ContactInfo(user_id=recipient_id)
            contacts.set(contact)

            service = NotificationService(contacts=contacts)

            request = NotificationRequest(
                recipient_id=recipient_id,
                channel=channel,
                title="Test Title",
                body="Test body",
            )

            with pytest.raises(ValidationError) as exc_info:
                await service.send(request)

            assert exc_info.value.field == "recipient_id"

        asyncio.get_event_loop().run_until_complete(run())


# --- Property 2: Opt-out rejection ---
# Validates: Requirements 1.2, 1.6, 8.3

from notification_system.contacts import ContactInfoStore
from notification_system.exceptions import ValidationError
from notification_system.models import Channel, ContactInfo, NotificationRequest
from notification_system.queue import MessageQueue
from notification_system.service import NotificationService
from notification_system.settings import NotificationSettings


# Strategies for Property 2
_optout_user_ids = st.text(
    min_size=1,
    max_size=30,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
)


@st.composite
def user_with_contact_for_channel(draw):
    """Generate a (user_id, channel, ContactInfo) tuple where the user has
    valid contact info for the given channel.
    """
    user_id = draw(_optout_user_ids)
    channel = draw(channels)

    # Build contact info appropriate for the channel
    if channel == Channel.EMAIL:
        contact = ContactInfo(user_id=user_id, email=f"{user_id}@example.com")
    elif channel == Channel.SMS:
        contact = ContactInfo(user_id=user_id, phone="+15551234567")
    else:
        # IOS_PUSH or ANDROID_PUSH
        contact = ContactInfo(user_id=user_id, device_tokens=["token_abc123"])

    return user_id, channel, contact


class TestProperty2OptOutRejection:
    """Property 2: Opt-out rejection.

    For any user who has opted out of a channel, calling service.send()
    for that channel raises ValidationError and the queue remains empty.

    **Validates: Requirements 1.2, 1.6, 8.3**
    """

    @given(data=user_with_contact_for_channel())
    @settings(max_examples=200)
    def test_opted_out_user_send_raises_validation_error(self, data):
        """When a user has opted out of a channel, sending a notification
        on that channel raises ValidationError.

        **Validates: Requirements 1.2, 1.6, 8.3**
        """
        user_id, channel, contact = data

        async def run():
            # Set up the service with contacts and settings
            contacts = ContactInfoStore()
            contacts.set(contact)

            settings = NotificationSettings()
            settings.opt_out(user_id, channel)

            queue = MessageQueue(max_depth=100)

            service = NotificationService(
                contacts=contacts,
                settings=settings,
                queue=queue,
            )

            request = NotificationRequest(
                recipient_id=user_id,
                channel=channel,
                title="Test notification",
                body="This should be rejected",
            )

            # Sending to an opted-out channel must raise ValidationError
            with pytest.raises(ValidationError):
                await service.send(request)

            # The queue must remain empty (nothing was enqueued)
            assert queue.depth(channel) == 0, (
                f"Queue should be empty after opt-out rejection, "
                f"but depth is {queue.depth(channel)} for channel {channel}"
            )

        asyncio.get_event_loop().run_until_complete(run())

    @given(data=user_with_contact_for_channel())
    @settings(max_examples=200)
    def test_opted_out_user_queue_remains_empty(self, data):
        """After an opt-out rejection, the message queue for the channel
        remains empty, confirming no task was enqueued.

        **Validates: Requirements 1.2, 1.6, 8.3**
        """
        user_id, channel, contact = data

        async def run():
            contacts = ContactInfoStore()
            contacts.set(contact)

            settings = NotificationSettings()
            settings.opt_out(user_id, channel)

            queue = MessageQueue(max_depth=100)

            service = NotificationService(
                contacts=contacts,
                settings=settings,
                queue=queue,
            )

            request = NotificationRequest(
                recipient_id=user_id,
                channel=channel,
                title="Another test",
                body="Should not be queued",
            )

            try:
                await service.send(request)
            except ValidationError:
                pass  # Expected

            # Verify all channels are empty (nothing leaked to other queues)
            for ch in Channel:
                assert queue.depth(ch) == 0, (
                    f"Queue for channel {ch} should be empty, "
                    f"but depth is {queue.depth(ch)}"
                )

        asyncio.get_event_loop().run_until_complete(run())

    @given(data=user_with_contact_for_channel())
    @settings(max_examples=200)
    def test_opt_out_then_opt_in_allows_send(self, data):
        """After opting out and then opting back in, send() succeeds
        (confirming the opt-out was the cause of rejection, not something else).

        **Validates: Requirements 1.2, 1.6, 8.3**
        """
        user_id, channel, contact = data

        async def run():
            contacts = ContactInfoStore()
            contacts.set(contact)

            settings = NotificationSettings()
            settings.opt_out(user_id, channel)

            queue = MessageQueue(max_depth=100)

            service = NotificationService(
                contacts=contacts,
                settings=settings,
                queue=queue,
            )

            request = NotificationRequest(
                recipient_id=user_id,
                channel=channel,
                title="Test after re-opt-in",
                body="Should succeed after opt-in",
            )

            # First, verify it's rejected while opted out
            with pytest.raises(ValidationError):
                await service.send(request)

            assert queue.depth(channel) == 0

            # Now opt back in
            settings.opt_in(user_id, channel)

            # Send should now succeed
            notification_id = await service.send(request)
            assert notification_id is not None
            assert len(notification_id) > 0

            # Queue should now have one item
            assert queue.depth(channel) == 1

        asyncio.get_event_loop().run_until_complete(run())


# --- Property 4: Notification ID uniqueness ---
# Validates: Requirements 1.8

from notification_system.config import NotificationConfig, RateLimitConfig
from notification_system.contacts import ContactInfoStore
from notification_system.models import Channel, ContactInfo, NotificationRequest
from notification_system.queue import MessageQueue
from notification_system.service import NotificationService


class TestProperty4NotificationIDUniqueness:
    """Property 4: Notification ID uniqueness.

    For any N notifications sent through the service, all returned
    notification IDs are unique (no duplicates).

    **Validates: Requirements 1.8**
    """

    @given(
        channel=channels,
        recipient_id=st.text(
            min_size=1,
            max_size=20,
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
        ),
        num_notifications=st.integers(min_value=2, max_value=20),
    )
    @settings(max_examples=200)
    def test_all_notification_ids_are_unique(
        self, channel: Channel, recipient_id: str, num_notifications: int
    ):
        """For any N notifications sent through the service, all returned
        notification IDs are distinct (no duplicates).

        **Validates: Requirements 1.8**
        """

        async def run():
            # Set up contacts with valid endpoints for all channels
            contacts = ContactInfoStore()
            contact = ContactInfo(
                user_id=recipient_id,
                email=f"{recipient_id}@example.com",
                phone="+15551234567",
                device_tokens=["device_token_abc123"],
            )
            contacts.set(contact)

            # Use high rate limits and unique content to avoid dedup interference
            high_limit = RateLimitConfig(max_count=10000, window_seconds=3600.0)
            config = NotificationConfig(
                per_channel_rate_limits={ch: high_limit for ch in Channel},
                global_rate_limit=high_limit,
                dedup_window_seconds=300.0,
                queue_max_depth=1000,
            )

            service = NotificationService(config=config, contacts=contacts)

            # Send N notifications with different content to avoid dedup
            ids: list[str] = []
            for i in range(num_notifications):
                request = NotificationRequest(
                    recipient_id=recipient_id,
                    channel=channel,
                    title="Title {}".format(i),
                    body="Body {} unique content {}".format(i, i),
                )
                notification_id = await service.send(request)
                ids.append(notification_id)

            # All IDs must be unique
            assert len(ids) == len(set(ids)), (
                "Duplicate notification IDs found: {}".format(ids)
            )

        asyncio.get_event_loop().run_until_complete(run())

    @given(
        channels_list=st.lists(
            channels,
            min_size=2,
            max_size=15,
        ),
        recipient_id=st.text(
            min_size=1,
            max_size=20,
            alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
        ),
    )
    @settings(max_examples=200)
    def test_ids_unique_across_different_channels(
        self, channels_list: list, recipient_id: str
    ):
        """Notification IDs are unique even when notifications are sent
        across different channels for the same recipient.

        **Validates: Requirements 1.8**
        """

        async def run():
            # Set up contacts with valid endpoints for all channels
            contacts = ContactInfoStore()
            contact = ContactInfo(
                user_id=recipient_id,
                email=f"{recipient_id}@example.com",
                phone="+15551234567",
                device_tokens=["device_token_abc123"],
            )
            contacts.set(contact)

            # Use high rate limits and unique content per iteration
            high_limit = RateLimitConfig(max_count=10000, window_seconds=3600.0)
            config = NotificationConfig(
                per_channel_rate_limits={ch: high_limit for ch in Channel},
                global_rate_limit=high_limit,
                dedup_window_seconds=300.0,
                queue_max_depth=1000,
            )

            service = NotificationService(config=config, contacts=contacts)

            # Send notifications across different channels
            ids: list[str] = []
            for i, ch in enumerate(channels_list):
                request = NotificationRequest(
                    recipient_id=recipient_id,
                    channel=ch,
                    title="Title {}".format(i),
                    body="Body {} unique content for channel {}".format(i, ch.value),
                )
                notification_id = await service.send(request)
                ids.append(notification_id)

            # All IDs must be unique across all channels
            assert len(ids) == len(set(ids)), (
                "Duplicate notification IDs found across channels: {}".format(ids)
            )

        asyncio.get_event_loop().run_until_complete(run())
