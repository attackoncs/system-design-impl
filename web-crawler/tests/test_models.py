"""Unit tests for data models."""

import pytest

from web_crawler.models import CrawlResult, CrawlStatus, FrontierEntry, Priority


class TestCrawlStatus:
    """Test CrawlStatus enum values."""

    def test_success_value(self):
        assert CrawlStatus.SUCCESS.value == "success"

    def test_error_value(self):
        assert CrawlStatus.ERROR.value == "error"

    def test_duplicate_value(self):
        assert CrawlStatus.DUPLICATE.value == "duplicate"

    def test_disallowed_value(self):
        assert CrawlStatus.DISALLOWED.value == "disallowed"

    def test_filtered_value(self):
        assert CrawlStatus.FILTERED.value == "filtered"


class TestPriority:
    """Test Priority enum values and ordering."""

    def test_high_value(self):
        assert Priority.HIGH.value == 0

    def test_medium_value(self):
        assert Priority.MEDIUM.value == 1

    def test_low_value(self):
        assert Priority.LOW.value == 2

    def test_high_less_than_medium(self):
        assert Priority.HIGH.value < Priority.MEDIUM.value

    def test_medium_less_than_low(self):
        assert Priority.MEDIUM.value < Priority.LOW.value


class TestFrontierEntry:
    """Test FrontierEntry auto-computes host and __lt__ ordering."""

    def test_auto_computes_host(self):
        entry = FrontierEntry(url="http://example.com/page")
        assert entry.host == "example.com"

    def test_auto_computes_host_with_port(self):
        entry = FrontierEntry(url="http://example.com:8080/page")
        assert entry.host == "example.com:8080"

    def test_host_lowercased(self):
        entry = FrontierEntry(url="http://EXAMPLE.COM/page")
        assert entry.host == "example.com"

    def test_explicit_host_not_overwritten(self):
        entry = FrontierEntry(url="http://example.com/page", host="custom.host")
        assert entry.host == "custom.host"

    def test_lt_high_before_medium(self):
        high = FrontierEntry(url="http://a.com/1", priority=Priority.HIGH)
        medium = FrontierEntry(url="http://b.com/2", priority=Priority.MEDIUM)
        assert high < medium

    def test_lt_medium_before_low(self):
        medium = FrontierEntry(url="http://a.com/1", priority=Priority.MEDIUM)
        low = FrontierEntry(url="http://b.com/2", priority=Priority.LOW)
        assert medium < low

    def test_lt_same_priority(self):
        a = FrontierEntry(url="http://a.com/1", priority=Priority.MEDIUM)
        b = FrontierEntry(url="http://b.com/2", priority=Priority.MEDIUM)
        # Same priority: not less than
        assert not (a < b)


class TestCrawlResult:
    """Test CrawlResult creation with defaults."""

    def test_minimal_creation(self):
        result = CrawlResult(url="http://example.com", status=CrawlStatus.SUCCESS)
        assert result.url == "http://example.com"
        assert result.status == CrawlStatus.SUCCESS
        assert result.http_status is None
        assert result.content_length is None
        assert result.content_fingerprint is None
        assert result.links == []
        assert result.depth == 0
        assert result.error_message is None
        assert result.headers == {}

    def test_full_creation(self):
        result = CrawlResult(
            url="http://example.com",
            status=CrawlStatus.SUCCESS,
            http_status=200,
            content_length=1024,
            content_fingerprint="abc123",
            links=["http://example.com/page1"],
            depth=2,
            headers={"Content-Type": "text/html"},
        )
        assert result.http_status == 200
        assert result.content_length == 1024
        assert result.links == ["http://example.com/page1"]
        assert result.depth == 2
