"""Tests for the Fixed Window Counter rate limiting algorithm."""

import time
import unittest
from unittest.mock import Mock, patch

from src.rate_limiter.algorithms.fixed_window import FixedWindowAlgorithm
from src.rate_limiter.config import Algorithm, RateLimitRule
from src.rate_limiter.storage.memory import MemoryStorage


class TestFixedWindowAlgorithm(unittest.TestCase):
    """Test suite for FixedWindowAlgorithm."""

    def setUp(self):
        """Set up test fixtures."""
        self.algorithm = FixedWindowAlgorithm()
        self.storage = MemoryStorage()
        self.rule = RateLimitRule(limit=5, window=60, algorithm=Algorithm.FIXED_WINDOW)
        self.key = "test_key"

    def test_allows_requests_within_limit(self):
        """Test that requests within the limit are allowed."""
        # Make 5 requests (within limit)
        for i in range(5):
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertTrue(result.allowed)
            self.assertEqual(result.remaining, 4 - i)
            self.assertEqual(result.limit, 5)

    def test_denies_requests_over_limit(self):
        """Test that requests over the limit are denied."""
        # Fill the window
        for i in range(5):
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertTrue(result.allowed)

        # 6th request should be denied
        result = self.algorithm.check(self.key, self.rule, self.storage)
        self.assertFalse(result.allowed)
        self.assertEqual(result.remaining, 0)
        self.assertEqual(result.limit, 5)
        self.assertIsNotNone(result.retry_after)
        self.assertGreater(result.retry_after, 0)

    def test_resets_at_window_boundary(self):
        """Test that the counter resets at window boundaries."""
        # Fill the current window
        for i in range(5):
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertTrue(result.allowed)

        # 6th request should be denied
        result = self.algorithm.check(self.key, self.rule, self.storage)
        self.assertFalse(result.allowed)

        # Simulate moving to next window by mocking time
        with patch('time.time', return_value=time.time() + 61):
            # Should be allowed again in new window
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertTrue(result.allowed)
            self.assertEqual(result.remaining, 4)
            self.assertEqual(result.limit, 5)

    def test_reset_after_calculation(self):
        """Test that reset_after shows correct time until window reset."""
        # Use a time that's clearly within a window (not at boundary)
        base_time = 1000030.0  # 10 seconds into a window (window: 1000020-1000080)

        with patch('time.time', return_value=base_time):
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertTrue(result.allowed)

            # Should reset after approximately 50 seconds (60 - 10 = 50)
            expected_reset = 50.0
            self.assertAlmostEqual(result.reset_after, expected_reset, delta=0.1)

        # Test at different point in window
        with patch('time.time', return_value=base_time + 20):
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertTrue(result.allowed)

            # Should reset after approximately 30 seconds (60 - 30 = 30)
            expected_reset = 30.0
            self.assertAlmostEqual(result.reset_after, expected_reset, delta=0.1)

    def test_retry_after_for_denied_requests(self):
        """Test that retry_after is set correctly for denied requests."""
        # Use a time that's clearly within a window (not at boundary)
        base_time = 1000030.0  # 10 seconds into a window (window: 1000020-1000080)

        # Fill the window
        for i in range(5):
            with patch('time.time', return_value=base_time):
                result = self.algorithm.check(self.key, self.rule, self.storage)
                self.assertTrue(result.allowed)

        # Try one more request (still within same window)
        with patch('time.time', return_value=base_time + 10):
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertFalse(result.allowed)

            # Should retry after approximately 40 seconds (60 - 20 = 40)
            expected_retry = 40.0
            self.assertAlmostEqual(result.retry_after, expected_retry, delta=0.1)

    def test_different_keys_are_isolated(self):
        """Test that different keys have independent counters."""
        key1 = "user1"
        key2 = "user2"

        # Fill up key1
        for i in range(5):
            result = self.algorithm.check(key1, self.rule, self.storage)
            self.assertTrue(result.allowed)

        # key2 should still be able to make requests
        result = self.algorithm.check(key2, self.rule, self.storage)
        self.assertTrue(result.allowed)
        self.assertEqual(result.remaining, 4)

    def test_different_rules_are_isolated(self):
        """Test that different rules affect the same key independently."""
        rule1 = RateLimitRule(limit=3, window=60, algorithm=Algorithm.FIXED_WINDOW)
        rule2 = RateLimitRule(limit=5, window=60, algorithm=Algorithm.FIXED_WINDOW)

        # Use up rule1
        for i in range(3):
            result = self.algorithm.check(self.key, rule1, self.storage)
            self.assertTrue(result.allowed)

        # Should be denied by rule1
        result = self.algorithm.check(self.key, rule1, self.storage)
        self.assertFalse(result.allowed)

        # But should still be allowed by rule2
        result = self.algorithm.check(self.key, rule2, self.storage)
        self.assertTrue(result.allowed)
        self.assertEqual(result.remaining, 4)

    def test_window_boundary_edge_case(self):
        """Test behavior right at window boundaries."""
        # Use a time that's clearly within a window
        base_time = 1000030.0  # 30 seconds into a window
        window_size = 60

        # Make requests at the end of a window (e.g., at time 59 seconds into window)
        with patch('time.time', return_value=base_time + 29):  # 59 seconds into window
            for i in range(5):
                result = self.algorithm.check(self.key, self.rule, self.storage)
                self.assertTrue(result.allowed)

        # Make a request at the beginning of next window
        with patch('time.time', return_value=base_time + 61):  # 1 second into next window
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertTrue(result.allowed)
            self.assertEqual(result.remaining, 4)

    def test_storage_ttl_management(self):
        """Test that storage keys have appropriate TTL."""
        # Make a request
        result = self.algorithm.check(self.key, self.rule, self.storage)
        self.assertTrue(result.allowed)

        # Check that keys exist in storage with TTL
        # Note: keys now include rule parameters to isolate different rules
        rule_suffix = ":60:5"
        counter_key = f"{self.key}{rule_suffix}:counter"
        window_start_key = f"{self.key}{rule_suffix}:window_start"

        # Keys should exist
        self.assertIsNotNone(self.storage._get_raw(counter_key))
        self.assertIsNotNone(self.storage._get_raw(window_start_key))

    def test_atomic_operations(self):
        """Test that operations are atomic under concurrent access."""
        import threading
        import time

        results = []
        errors = []

        def make_request():
            try:
                result = self.algorithm.check(self.key, self.rule, self.storage)
                results.append(result)
            except Exception as e:
                errors.append(e)

        # Start multiple threads simultaneously
        threads = []
        for i in range(10):
            thread = threading.Thread(target=make_request)
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        # Should have no errors
        self.assertEqual(len(errors), 0)

        # Should have 10 results
        self.assertEqual(len(results), 10)

        # Exactly 5 should be allowed, 5 denied
        allowed_count = sum(1 for r in results if r.allowed)
        denied_count = sum(1 for r in results if not r.allowed)

        self.assertEqual(allowed_count, 5)
        self.assertEqual(denied_count, 5)

        # All allowed requests should have decreasing remaining counts
        allowed_results = [r for r in results if r.allowed]
        remaining_counts = [r.remaining for r in allowed_results]

        # Should have 5 unique remaining counts: 4, 3, 2, 1, 0
        self.assertEqual(set(remaining_counts), {0, 1, 2, 3, 4})


if __name__ == '__main__':
    unittest.main()
