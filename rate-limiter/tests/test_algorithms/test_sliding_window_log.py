"""Tests for the Sliding Window Log rate limiting algorithm."""

import time
import unittest
from unittest.mock import Mock, patch

from src.rate_limiter.algorithms.sliding_window_log import SlidingWindowLogAlgorithm
from src.rate_limiter.config import Algorithm, RateLimitRule
from src.rate_limiter.storage.memory import MemoryStorage


class TestSlidingWindowLogAlgorithm(unittest.TestCase):
    """Test suite for SlidingWindowLogAlgorithm."""

    def setUp(self):
        """Set up test fixtures."""
        self.algorithm = SlidingWindowLogAlgorithm()
        self.storage = MemoryStorage()
        self.rule = RateLimitRule(limit=5, window=60, algorithm=Algorithm.SLIDING_WINDOW_LOG)
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

    def test_old_entries_expire(self):
        """Test that old entries are removed from the log as they expire."""
        base_time = 1000000.0

        # Fill the window at time 0
        with patch('time.time', return_value=base_time):
            for i in range(5):
                result = self.algorithm.check(self.key, self.rule, self.storage)
                self.assertTrue(result.allowed)

        # Try another request at the same time - should be denied
        with patch('time.time', return_value=base_time):
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertFalse(result.allowed)

        # After window expires, should allow requests again
        with patch('time.time', return_value=base_time + 61):
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertTrue(result.allowed)
            self.assertEqual(result.remaining, 4)

    def test_partial_expiry(self):
        """Test that only expired entries are removed, not all entries."""
        base_time = 1000000.0

        # Make requests spread over time
        for i in range(5):
            with patch('time.time', return_value=base_time + i * 10):
                result = self.algorithm.check(self.key, self.rule, self.storage)
                self.assertTrue(result.allowed)

        # All 5 requests should be in the window
        with patch('time.time', return_value=base_time + 30):
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertFalse(result.allowed)

        # After first request expires (at time 60), should allow one more
        with patch('time.time', return_value=base_time + 61):
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertTrue(result.allowed)
            self.assertEqual(result.remaining, 0)  # Still at limit, but sliding

    def test_reset_after_calculation(self):
        """Test that reset_after shows correct time until oldest entry expires."""
        base_time = 1000000.0

        with patch('time.time', return_value=base_time):
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertTrue(result.allowed)

            # Should reset after window duration
            expected_reset = 60.0
            self.assertAlmostEqual(result.reset_after, expected_reset, delta=0.1)

        # Make another request 30 seconds later
        with patch('time.time', return_value=base_time + 30):
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertTrue(result.allowed)

            # Should reset after 30 seconds (when first request expires)
            expected_reset = 30.0
            self.assertAlmostEqual(result.reset_after, expected_reset, delta=0.1)

    def test_retry_after_for_denied_requests(self):
        """Test that retry_after is set correctly for denied requests."""
        base_time = 1000000.0

        # Fill the window
        for i in range(5):
            with patch('time.time', return_value=base_time + i):
                result = self.algorithm.check(self.key, self.rule, self.storage)
                self.assertTrue(result.allowed)

        # Try one more request
        with patch('time.time', return_value=base_time + 30):
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertFalse(result.allowed)

            # Should retry after approximately 30 seconds (when first request expires)
            expected_retry = 30.0
            self.assertAlmostEqual(result.retry_after, expected_retry, delta=0.1)

    def test_different_keys_are_isolated(self):
        """Test that different keys have independent logs."""
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
        rule1 = RateLimitRule(limit=3, window=60, algorithm=Algorithm.SLIDING_WINDOW_LOG)
        rule2 = RateLimitRule(limit=5, window=60, algorithm=Algorithm.SLIDING_WINDOW_LOG)

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

    def test_precise_sliding_behavior(self):
        """Test the precise sliding behavior - allows smooth rate limiting."""
        base_time = 1000000.0

        # Make requests at regular intervals
        for i in range(5):
            with patch('time.time', return_value=base_time + i * 12):  # Every 12 seconds
                result = self.algorithm.check(self.key, self.rule, self.storage)
                self.assertTrue(result.allowed)

        # Should be denied now
        with patch('time.time', return_value=base_time + 50):
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertFalse(result.allowed)

        # After first request expires (at 60 seconds), should allow one more
        with patch('time.time', return_value=base_time + 61):
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertTrue(result.allowed)

        # But should be denied again immediately
        with patch('time.time', return_value=base_time + 61.1):
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertFalse(result.allowed)

    def test_storage_ttl_management(self):
        """Test that storage keys have appropriate TTL."""
        # Make a request
        result = self.algorithm.check(self.key, self.rule, self.storage)
        self.assertTrue(result.allowed)

        # Check that log key exists in storage
        # Note: keys now include rule parameters to isolate different rules
        rule_suffix = ":60:5"
        log_key = f"{self.key}{rule_suffix}:log"

        # Key should exist
        self.assertIsNotNone(self.storage._get_raw(log_key))

    def test_atomic_operations(self):
        """Test that operations are atomic under concurrent access."""
        import threading

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

    def test_empty_log_behavior(self):
        """Test behavior when log is empty."""
        base_time = 1000000.0

        # First request should always be allowed
        with patch('time.time', return_value=base_time):
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertTrue(result.allowed)
            self.assertEqual(result.remaining, 4)
            self.assertEqual(result.limit, 5)

    def test_window_edge_cases(self):
        """Test behavior at exact window boundaries."""
        base_time = 1000000.0

        # Make a request at exact window boundary
        with patch('time.time', return_value=base_time):
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertTrue(result.allowed)

        # Make another request exactly at window + 1
        with patch('time.time', return_value=base_time + 60):
            result = self.algorithm.check(self.key, self.rule, self.storage)
            self.assertTrue(result.allowed)
            # First request should still be in the window (not expired yet)
            # But since we're at exactly window boundary, first request expires
            self.assertEqual(result.remaining, 4)


if __name__ == '__main__':
    unittest.main()
