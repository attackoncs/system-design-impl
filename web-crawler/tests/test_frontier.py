"""Unit tests for URLFrontier."""

import time
from unittest.mock import patch

import pytest

from web_crawler.frontier import URLFrontier
from web_crawler.models import FrontierEntry, Priority


class TestURLFrontierBasic:
    """Test put/get basic operation."""

    def test_put_and_get(self):
        frontier = URLFrontier(per_host_delay=0.0)
        entry = FrontierEntry(url="http://example.com/page1")
        frontier.put(entry)
        result = frontier.get()
        assert result is entry

    def test_get_empty_returns_none(self):
        frontier = URLFrontier(per_host_delay=0.0)
        assert frontier.get() is None


class TestURLFrontierPriority:
    """Test priority ordering (HIGH before MEDIUM before LOW) with different hosts."""

    def test_high_before_medium(self):
        frontier = URLFrontier(per_host_delay=0.0)
        medium = FrontierEntry(url="http://b.com/page", priority=Priority.MEDIUM)
        high = FrontierEntry(url="http://a.com/page", priority=Priority.HIGH)
        frontier.put(medium)
        frontier.put(high)
        assert frontier.get() is high
        assert frontier.get() is medium

    def test_priority_ordering_all_levels(self):
        frontier = URLFrontier(per_host_delay=0.0)
        low = FrontierEntry(url="http://c.com/page", priority=Priority.LOW)
        high = FrontierEntry(url="http://a.com/page", priority=Priority.HIGH)
        medium = FrontierEntry(url="http://b.com/page", priority=Priority.MEDIUM)
        frontier.put(low)
        frontier.put(high)
        frontier.put(medium)
        assert frontier.get() is high
        assert frontier.get() is medium
        assert frontier.get() is low


class TestURLFrontierSizeAndEmpty:
    """Test is_empty and size."""

    def test_empty_initially(self):
        frontier = URLFrontier()
        assert frontier.is_empty()
        assert frontier.size == 0

    def test_not_empty_after_put(self):
        frontier = URLFrontier()
        frontier.put(FrontierEntry(url="http://example.com/page"))
        assert not frontier.is_empty()
        assert frontier.size == 1

    def test_empty_after_get(self):
        frontier = URLFrontier(per_host_delay=0.0)
        frontier.put(FrontierEntry(url="http://example.com/page"))
        frontier.get()
        assert frontier.is_empty()
        assert frontier.size == 0


class TestURLFrontierPoliteness:
    """Test politeness delay (same host, get returns None before delay)."""

    def test_same_host_blocked_by_delay(self):
        frontier = URLFrontier(per_host_delay=10.0)
        entry1 = FrontierEntry(url="http://example.com/page1")
        entry2 = FrontierEntry(url="http://example.com/page2")
        frontier.put(entry1)
        frontier.put(entry2)

        # First get should succeed
        result1 = frontier.get()
        assert result1 is entry1

        # Second get should return None (same host, delay not elapsed)
        result2 = frontier.get()
        assert result2 is None

    def test_different_hosts_not_blocked(self):
        frontier = URLFrontier(per_host_delay=10.0)
        entry1 = FrontierEntry(url="http://a.com/page1")
        entry2 = FrontierEntry(url="http://b.com/page2")
        frontier.put(entry1)
        frontier.put(entry2)

        result1 = frontier.get()
        assert result1 is not None
        result2 = frontier.get()
        assert result2 is not None
