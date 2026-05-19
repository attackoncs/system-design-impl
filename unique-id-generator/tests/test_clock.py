"""Tests for Clock implementations."""

import time

from unique_id.clock import Clock, SystemClock, MonotonicClock


class TestSystemClock:
    """Test SystemClock implementation."""

    def test_returns_reasonable_value(self):
        clock = SystemClock()
        now_ms = int(time.time() * 1000)
        result = clock.current_millis()
        # Should be within 100ms of system time
        assert abs(result - now_ms) < 100

    def test_approximately_equals_system_time(self):
        clock = SystemClock()
        before = int(time.time() * 1000)
        result = clock.current_millis()
        after = int(time.time() * 1000)
        assert before <= result <= after

    def test_implements_clock_interface(self):
        clock = SystemClock()
        assert isinstance(clock, Clock)


class TestMonotonicClock:
    """Test MonotonicClock implementation."""

    def test_returns_reasonable_value(self):
        clock = MonotonicClock()
        now_ms = int(time.time() * 1000)
        result = clock.current_millis()
        # Should be within 100ms of system time
        assert abs(result - now_ms) < 100

    def test_monotonically_increasing(self):
        clock = MonotonicClock()
        values = [clock.current_millis() for _ in range(100)]
        for i in range(1, len(values)):
            assert values[i] >= values[i - 1]

    def test_implements_clock_interface(self):
        clock = MonotonicClock()
        assert isinstance(clock, Clock)
