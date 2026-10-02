"""Unit tests for CrawlerConfig."""

import pytest

from web_crawler.config import CrawlerConfig


class TestCrawlerConfigDefaults:
    """Test default configuration values."""

    def test_default_max_pages(self):
        config = CrawlerConfig()
        assert config.max_pages == 100

    def test_default_max_depth(self):
        config = CrawlerConfig()
        assert config.max_depth == 3

    def test_default_max_concurrent(self):
        config = CrawlerConfig()
        assert config.max_concurrent == 10

    def test_default_per_host_delay(self):
        config = CrawlerConfig()
        assert config.per_host_delay == 1.0

    def test_default_request_timeout(self):
        config = CrawlerConfig()
        assert config.request_timeout == 30.0

    def test_default_user_agent(self):
        config = CrawlerConfig()
        assert config.user_agent == "WebCrawler/1.0"

    def test_default_robots_cache_ttl(self):
        config = CrawlerConfig()
        assert config.robots_cache_ttl == 3600.0

    def test_default_max_url_length(self):
        config = CrawlerConfig()
        assert config.max_url_length == 2048


class TestCrawlerConfigValidation:
    """Test validation raises ValueError for non-positive numerics."""

    def test_invalid_max_pages_zero(self):
        with pytest.raises(ValueError, match="max_pages"):
            CrawlerConfig(max_pages=0)

    def test_invalid_max_pages_negative(self):
        with pytest.raises(ValueError, match="max_pages"):
            CrawlerConfig(max_pages=-1)

    def test_invalid_max_depth(self):
        with pytest.raises(ValueError, match="max_depth"):
            CrawlerConfig(max_depth=0)

    def test_invalid_max_concurrent(self):
        with pytest.raises(ValueError, match="max_concurrent"):
            CrawlerConfig(max_concurrent=-5)

    def test_invalid_per_host_delay(self):
        with pytest.raises(ValueError, match="per_host_delay"):
            CrawlerConfig(per_host_delay=0)

    def test_invalid_request_timeout(self):
        with pytest.raises(ValueError, match="request_timeout"):
            CrawlerConfig(request_timeout=-1.0)

    def test_invalid_max_url_length(self):
        with pytest.raises(ValueError, match="max_url_length"):
            CrawlerConfig(max_url_length=0)

    def test_invalid_robots_cache_ttl(self):
        with pytest.raises(ValueError, match="robots_cache_ttl"):
            CrawlerConfig(robots_cache_ttl=-100)
