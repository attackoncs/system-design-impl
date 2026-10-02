"""Unit tests for the sliding window rate limiter.

Tests per-channel limits, global limits, sliding window behavior,
RateLimitExceededError raised when limit exceeded, check passes
before limit, and injectable clock for time control.

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6
"""

from __future__ import annotations

from typing import Dict, Optional

import pytest

from notification_system.config import RateLimitConfig
from notification_system.exceptions import RateLimitExceededError
from notification_system.models import Channel
from notification_system.rate_limiter import SlidingWindowRateLimiter


def make_limiter(
    per_channel: Optional[Dict[Channel, RateLimitConfig]] = None,
    global_limit: Optional[RateLimitConfig] = None,
    start_time: float = 0.0,
):
    """Helper to create a rate limiter with a controllable clock."""
    current_time = [start_time]

    def clock() -> float:
        return current_time[0]

    def advance(seconds: float) -> None:
        current_time[0] += seconds

    if per_channel is None:
        per_channel = {
            Channel.EMAIL: RateLimitConfig(max_count=3, window_seconds=60.0),
            Channel.SMS: RateLimitConfig(max_count=2, window_seconds=60.0),
            Channel.IOS_PUSH: RateLimitConfig(max_count=5, window_seconds=60.0),
            Channel.ANDROID_PUSH: RateLimitConfig(max_count=5, window_seconds=60.0),
        }
    if global_limit is None:
        global_limit = RateLimitConfig(max_count=10, window_seconds=60.0)

    limiter = SlidingWindowRateLimiter(per_channel, global_limit, clock=clock)
    return limiter, advance


class TestPerChannelLimits:
    """Tests for per-channel rate limit enforcement (Req 6.1, 6.2)."""

    def test_check_passes_before_limit(self):
        """Notifications below the per-channel limit should pass."""
        limiter, _ = make_limiter()
        # EMAIL limit is 3 per 60s; sending 3 should all pass check
        for _ in range(3):
            limiter.check("user1", Channel.EMAIL)
            limiter.record("user1", Channel.EMAIL)

    def test_exceeds_per_channel_limit(self):
        """Exceeding per-channel limit raises RateLimitExceededError."""
        limiter, _ = make_limiter()
        # EMAIL limit is 3
        for _ in range(3):
            limiter.check("user1", Channel.EMAIL)
            limiter.record("user1", Channel.EMAIL)

        with pytest.raises(RateLimitExceededError) as exc_info:
            limiter.check("user1", Channel.EMAIL)

        assert exc_info.value.user_id == "user1"
        assert exc_info.value.channel == "email"
        assert exc_info.value.limit == 3
        assert exc_info.value.window == 60.0

    def test_different_channels_independent(self):
        """Per-channel limits are independent of each other (Req 6.2)."""
        limiter, _ = make_limiter()
        # Fill up EMAIL limit (3)
        for _ in range(3):
            limiter.check("user1", Channel.EMAIL)
            limiter.record("user1", Channel.EMAIL)

        # SMS should still be allowed (separate limit)
        limiter.check("user1", Channel.SMS)
        limiter.record("user1", Channel.SMS)

    def test_different_users_independent(self):
        """Rate limits are per-user; one user's usage doesn't affect another."""
        limiter, _ = make_limiter()
        # Fill up user1's EMAIL limit
        for _ in range(3):
            limiter.check("user1", Channel.EMAIL)
            limiter.record("user1", Channel.EMAIL)

        # user2 should still be allowed
        limiter.check("user2", Channel.EMAIL)
        limiter.record("user2", Channel.EMAIL)

    def test_sms_channel_limit(self):
        """SMS has its own limit (2 per 60s)."""
        limiter, _ = make_limiter()
        for _ in range(2):
            limiter.check("user1", Channel.SMS)
            limiter.record("user1", Channel.SMS)

        with pytest.raises(RateLimitExceededError) as exc_info:
            limiter.check("user1", Channel.SMS)

        assert exc_info.value.channel == "sms"
        assert exc_info.value.limit == 2


class TestGlobalLimits:
    """Tests for global (cross-channel) rate limit enforcement (Req 6.3)."""

    def test_global_limit_across_channels(self):
        """Global limit applies across all channels combined."""
        # Set high per-channel limits but low global limit
        per_channel = {
            Channel.EMAIL: RateLimitConfig(max_count=100, window_seconds=60.0),
            Channel.SMS: RateLimitConfig(max_count=100, window_seconds=60.0),
            Channel.IOS_PUSH: RateLimitConfig(max_count=100, window_seconds=60.0),
            Channel.ANDROID_PUSH: RateLimitConfig(max_count=100, window_seconds=60.0),
        }
        global_limit = RateLimitConfig(max_count=5, window_seconds=60.0)
        limiter, _ = make_limiter(per_channel=per_channel, global_limit=global_limit)

        # Send across different channels
        channels = [Channel.EMAIL, Channel.SMS, Channel.IOS_PUSH, Channel.ANDROID_PUSH, Channel.EMAIL]
        for ch in channels:
            limiter.check("user1", ch)
            limiter.record("user1", ch)

        # 6th notification on any channel should hit global limit
        with pytest.raises(RateLimitExceededError) as exc_info:
            limiter.check("user1", Channel.SMS)

        assert exc_info.value.channel == "global"
        assert exc_info.value.limit == 5

    def test_global_limit_per_user(self):
        """Global limit is per-user; different users have separate global counts."""
        per_channel = {
            Channel.EMAIL: RateLimitConfig(max_count=100, window_seconds=60.0),
        }
        global_limit = RateLimitConfig(max_count=2, window_seconds=60.0)
        limiter, _ = make_limiter(per_channel=per_channel, global_limit=global_limit)

        # Fill user1's global limit
        for _ in range(2):
            limiter.check("user1", Channel.EMAIL)
            limiter.record("user1", Channel.EMAIL)

        # user2 should still be fine
        limiter.check("user2", Channel.EMAIL)
        limiter.record("user2", Channel.EMAIL)


class TestSlidingWindow:
    """Tests for sliding window time-based behavior (Req 6.6)."""

    def test_window_expiry_allows_new_notifications(self):
        """After the window expires, old timestamps are pruned and new sends allowed."""
        limiter, advance = make_limiter()
        # Fill EMAIL limit (3 per 60s)
        for _ in range(3):
            limiter.check("user1", Channel.EMAIL)
            limiter.record("user1", Channel.EMAIL)

        # Should be blocked now
        with pytest.raises(RateLimitExceededError):
            limiter.check("user1", Channel.EMAIL)

        # Advance past the window
        advance(61.0)

        # Should be allowed again
        limiter.check("user1", Channel.EMAIL)
        limiter.record("user1", Channel.EMAIL)

    def test_partial_window_expiry(self):
        """Only timestamps outside the window are pruned; recent ones remain."""
        per_channel = {
            Channel.EMAIL: RateLimitConfig(max_count=3, window_seconds=60.0),
        }
        global_limit = RateLimitConfig(max_count=100, window_seconds=60.0)
        limiter, advance = make_limiter(per_channel=per_channel, global_limit=global_limit)

        # Send 2 at t=0
        limiter.check("user1", Channel.EMAIL)
        limiter.record("user1", Channel.EMAIL)
        limiter.check("user1", Channel.EMAIL)
        limiter.record("user1", Channel.EMAIL)

        # Advance 30s, send 1 more (at t=30)
        advance(30.0)
        limiter.check("user1", Channel.EMAIL)
        limiter.record("user1", Channel.EMAIL)

        # At t=30, we have 3 in window -> should be blocked
        with pytest.raises(RateLimitExceededError):
            limiter.check("user1", Channel.EMAIL)

        # Advance to t=61 -> the 2 from t=0 expire, but t=30 one remains
        advance(31.0)

        # Now only 1 in window, can send 2 more
        limiter.check("user1", Channel.EMAIL)
        limiter.record("user1", Channel.EMAIL)
        limiter.check("user1", Channel.EMAIL)
        limiter.record("user1", Channel.EMAIL)

        # 3rd should fail (1 from t=30 + 2 new = 3 already)
        with pytest.raises(RateLimitExceededError):
            limiter.check("user1", Channel.EMAIL)

    def test_global_window_expiry(self):
        """Global limit also uses sliding window with expiry."""
        per_channel = {
            Channel.EMAIL: RateLimitConfig(max_count=100, window_seconds=60.0),
        }
        global_limit = RateLimitConfig(max_count=2, window_seconds=30.0)
        limiter, advance = make_limiter(per_channel=per_channel, global_limit=global_limit)

        # Fill global limit
        limiter.check("user1", Channel.EMAIL)
        limiter.record("user1", Channel.EMAIL)
        limiter.check("user1", Channel.EMAIL)
        limiter.record("user1", Channel.EMAIL)

        with pytest.raises(RateLimitExceededError):
            limiter.check("user1", Channel.EMAIL)

        # Advance past global window
        advance(31.0)

        # Should be allowed again
        limiter.check("user1", Channel.EMAIL)
        limiter.record("user1", Channel.EMAIL)


class TestRateLimitExceededError:
    """Tests for RateLimitExceededError details (Req 6.4)."""

    def test_error_contains_user_id(self):
        """Error includes the user_id that was rate limited."""
        limiter, _ = make_limiter()
        for _ in range(3):
            limiter.check("alice", Channel.EMAIL)
            limiter.record("alice", Channel.EMAIL)

        with pytest.raises(RateLimitExceededError) as exc_info:
            limiter.check("alice", Channel.EMAIL)

        assert exc_info.value.user_id == "alice"

    def test_error_contains_channel(self):
        """Error includes the channel name for per-channel violations."""
        limiter, _ = make_limiter()
        for _ in range(2):
            limiter.check("bob", Channel.SMS)
            limiter.record("bob", Channel.SMS)

        with pytest.raises(RateLimitExceededError) as exc_info:
            limiter.check("bob", Channel.SMS)

        assert exc_info.value.channel == "sms"

    def test_error_contains_limit_and_window(self):
        """Error includes the limit count and window duration."""
        limiter, _ = make_limiter()
        for _ in range(3):
            limiter.check("user1", Channel.EMAIL)
            limiter.record("user1", Channel.EMAIL)

        with pytest.raises(RateLimitExceededError) as exc_info:
            limiter.check("user1", Channel.EMAIL)

        assert exc_info.value.limit == 3
        assert exc_info.value.window == 60.0

    def test_error_message_is_descriptive(self):
        """Error message includes user, channel, limit, and window info."""
        limiter, _ = make_limiter()
        for _ in range(3):
            limiter.check("user1", Channel.EMAIL)
            limiter.record("user1", Channel.EMAIL)

        with pytest.raises(RateLimitExceededError) as exc_info:
            limiter.check("user1", Channel.EMAIL)

        msg = str(exc_info.value)
        assert "user1" in msg
        assert "email" in msg

    def test_error_inherits_from_notification_error(self):
        """RateLimitExceededError is a NotificationError."""
        from notification_system.exceptions import NotificationError

        limiter, _ = make_limiter()
        for _ in range(3):
            limiter.check("user1", Channel.EMAIL)
            limiter.record("user1", Channel.EMAIL)

        with pytest.raises(NotificationError):
            limiter.check("user1", Channel.EMAIL)


class TestInjectableClock:
    """Tests for injectable clock support for deterministic testing."""

    def test_custom_clock_is_used(self):
        """The limiter uses the injected clock, not real time."""
        call_count = [0]

        def counting_clock() -> float:
            call_count[0] += 1
            return 100.0

        per_channel = {Channel.EMAIL: RateLimitConfig(max_count=5, window_seconds=60.0)}
        global_limit = RateLimitConfig(max_count=10, window_seconds=60.0)
        limiter = SlidingWindowRateLimiter(per_channel, global_limit, clock=counting_clock)

        limiter.check("user1", Channel.EMAIL)
        limiter.record("user1", Channel.EMAIL)

        # Clock should have been called
        assert call_count[0] > 0

    def test_default_clock_uses_monotonic(self):
        """Without an injected clock, the limiter uses time.monotonic."""
        per_channel = {Channel.EMAIL: RateLimitConfig(max_count=5, window_seconds=60.0)}
        global_limit = RateLimitConfig(max_count=10, window_seconds=60.0)
        limiter = SlidingWindowRateLimiter(per_channel, global_limit)

        # Should not raise - just verifying it works with default clock
        limiter.check("user1", Channel.EMAIL)
        limiter.record("user1", Channel.EMAIL)

    def test_clock_controls_window_boundaries(self):
        """Advancing the clock past the window allows new notifications."""
        current_time = [0.0]

        def clock() -> float:
            return current_time[0]

        per_channel = {Channel.EMAIL: RateLimitConfig(max_count=1, window_seconds=10.0)}
        global_limit = RateLimitConfig(max_count=100, window_seconds=60.0)
        limiter = SlidingWindowRateLimiter(per_channel, global_limit, clock=clock)

        limiter.check("user1", Channel.EMAIL)
        limiter.record("user1", Channel.EMAIL)

        # Blocked at t=0
        with pytest.raises(RateLimitExceededError):
            limiter.check("user1", Channel.EMAIL)

        # Advance clock past window
        current_time[0] = 11.0

        # Now allowed
        limiter.check("user1", Channel.EMAIL)
        limiter.record("user1", Channel.EMAIL)


class TestCheckWithoutRecord:
    """Tests verifying check vs record separation."""

    def test_check_alone_does_not_consume_quota(self):
        """Calling check() without record() does not use up the limit."""
        limiter, _ = make_limiter()
        # Check many times without recording
        for _ in range(10):
            limiter.check("user1", Channel.EMAIL)

        # Should still be able to record
        limiter.record("user1", Channel.EMAIL)
        limiter.check("user1", Channel.EMAIL)

    def test_record_without_check_still_counts(self):
        """Recording without checking still fills the window."""
        limiter, _ = make_limiter()
        # Record 3 times (EMAIL limit) without checking
        for _ in range(3):
            limiter.record("user1", Channel.EMAIL)

        # Now check should fail
        with pytest.raises(RateLimitExceededError):
            limiter.check("user1", Channel.EMAIL)
