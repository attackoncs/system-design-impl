"""Unit tests for CrawlerConfig."""

import pytest
import web_crawler.config


class TestCrawlerConfigDefaults:
    """Test default configuration values."""

    def test_default_max_pages(self):
        config = web_crawler.config.CrawlerConfig()
        assert config.max_pages == 100

    def test_default_max_depth(self):
        config = web_crawler.config.CrawlerConfig()
        assert config.max_depth == 3

    def test_default_max_concurrent(self):
        config = web_crawler.config.CrawlerConfig()
        assert config.max_concurrent == 10

    def test_default_per_host_delay(self):
        config = web_crawler.config.CrawlerConfig()
        assert config.per_host_delay == 1.0

    def test_default_request_timeout(self):
        config = web_crawler.config.CrawlerConfig()
        assert config.request_timeout == 30.0

    def test_default_user_agent(self):
        config = web_crawler.config.CrawlerConfig()
        assert config.user_agent == "WebCrawler/1.0"

    def test_default_robots_cache_ttl(self):
        config = web_crawler.config.CrawlerConfig()
        assert config.robots_cache_ttl == 3600.0

    def test_default_max_url_length(self):
        config = web_crawler.config.CrawlerConfig()
        assert config.max_url_length == 2048


class TestCrawlerConfigValidation:
    """Test validation raises ValueError for non-positive numerics."""

    def test_invalid_max_pages_zero(self):
        with pytest.raises(ValueError, match="max_pages"):
            web_crawler.config.CrawlerConfig(max_pages=0)

    def test_invalid_max_pages_negative(self):
        with pytest.raises(ValueError, match="max_pages"):
            web_crawler.config.CrawlerConfig(max_pages=-1)

    def test_invalid_max_depth(self):
        with pytest.raises(ValueError, match="max_depth"):
            web_crawler.config.CrawlerConfig(max_depth=0)

    def test_invalid_max_concurrent(self):
        with pytest.raises(ValueError, match="max_concurrent"):
            web_crawler.config.CrawlerConfig(max_concurrent=-5)

    def test_invalid_per_host_delay(self):
        with pytest.raises(ValueError, match="per_host_delay"):
            web_crawler.config.CrawlerConfig(per_host_delay=0)

    def test_invalid_request_timeout(self):
        with pytest.raises(ValueError, match="request_timeout"):
            web_crawler.config.CrawlerConfig(request_timeout=-1.0)

    def test_invalid_max_url_length(self):
        with pytest.raises(ValueError, match="max_url_length"):
            web_crawler.config.CrawlerConfig(max_url_length=0)

    def test_invalid_robots_cache_ttl(self):
        with pytest.raises(ValueError, match="robots_cache_ttl"):
            web_crawler.config.CrawlerConfig(robots_cache_ttl=-100)
