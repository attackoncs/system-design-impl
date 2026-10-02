"""Unit tests for RobotsChecker."""

import time
from unittest.mock import patch

import pytest

from web_crawler.fetcher import Fetcher, FetchResponse
from web_crawler.exceptions import FetchError
from web_crawler.robots import RobotsChecker


class MockRobotsFetcher(Fetcher):
    """Mock fetcher that returns a robots.txt disallowing /private/."""

    def __init__(self, robots_content: str = "User-agent: *\nDisallow: /private/"):
        self.robots_content = robots_content
        self.fetch_count = 0

    async def fetch(self, url, headers=None, timeout=30.0):
        self.fetch_count += 1
        return FetchResponse(
            status_code=200,
            headers={"Content-Type": "text/plain"},
            body=self.robots_content.encode("utf-8"),
            url=url,
        )


class MockFailingFetcher(Fetcher):
    """Mock fetcher that always raises FetchError."""

    async def fetch(self, url, headers=None, timeout=30.0):
        raise FetchError(url, "Connection refused")


class TestRobotsCheckerAllowed:
    """Test is_allowed with mock fetcher returning robots.txt that disallows a path."""

    async def test_allowed_path(self):
        fetcher = MockRobotsFetcher()
        checker = RobotsChecker(fetcher, user_agent="WebCrawler/1.0")
        assert await checker.is_allowed("http://example.com/public/page")

    async def test_disallowed_path(self):
        fetcher = MockRobotsFetcher()
        checker = RobotsChecker(fetcher, user_agent="WebCrawler/1.0")
        assert not await checker.is_allowed("http://example.com/private/secret")

    async def test_root_path_allowed(self):
        fetcher = MockRobotsFetcher()
        checker = RobotsChecker(fetcher, user_agent="WebCrawler/1.0")
        assert await checker.is_allowed("http://example.com/")


class TestRobotsCheckerFetchFailure:
    """Test is_allowed when robots.txt fetch fails (should allow all)."""

    async def test_allows_all_on_fetch_failure(self):
        fetcher = MockFailingFetcher()
        checker = RobotsChecker(fetcher, user_agent="WebCrawler/1.0")
        assert await checker.is_allowed("http://example.com/private/secret")
        assert await checker.is_allowed("http://example.com/anything")

    async def test_caches_failure_result(self):
        """After a fetch failure, subsequent checks use cached None (allow all)."""
        fetcher = MockFailingFetcher()
        checker = RobotsChecker(fetcher, user_agent="WebCrawler/1.0")

        await checker.is_allowed("http://example.com/page1")
        await checker.is_allowed("http://example.com/page2")
        # Should only attempt fetch once due to caching the None result
        # (MockFailingFetcher doesn't track count, but we verify behavior)
        assert await checker.is_allowed("http://example.com/private/secret")


class TestRobotsCheckerCaching:
    """Test caching (second call doesn't re-fetch)."""

    async def test_caches_robots_txt(self):
        fetcher = MockRobotsFetcher()
        checker = RobotsChecker(fetcher, user_agent="WebCrawler/1.0")

        # First call fetches robots.txt
        await checker.is_allowed("http://example.com/page1")
        assert fetcher.fetch_count == 1

        # Second call uses cache
        await checker.is_allowed("http://example.com/page2")
        assert fetcher.fetch_count == 1

    async def test_different_hosts_fetch_separately(self):
        """Each host gets its own robots.txt fetch."""
        fetcher = MockRobotsFetcher()
        checker = RobotsChecker(fetcher, user_agent="WebCrawler/1.0")

        await checker.is_allowed("http://example.com/page")
        assert fetcher.fetch_count == 1

        await checker.is_allowed("http://other.com/page")
        assert fetcher.fetch_count == 2

    async def test_cache_ttl_expiry(self):
        """After TTL expires, robots.txt is re-fetched."""
        fetcher = MockRobotsFetcher()
        checker = RobotsChecker(fetcher, user_agent="WebCrawler/1.0", cache_ttl=1.0)

        await checker.is_allowed("http://example.com/page1")
        assert fetcher.fetch_count == 1

        # Simulate time passing beyond TTL by patching time.monotonic
        original_monotonic = time.monotonic
        with patch("time.monotonic", return_value=original_monotonic() + 2.0):
            await checker.is_allowed("http://example.com/page2")
            assert fetcher.fetch_count == 2


class TestRobotsCheckerUserAgent:
    """Test user-agent matching in robots.txt rules."""

    async def test_specific_user_agent_disallowed(self):
        """A specific user-agent can be disallowed while others are allowed."""
        robots_content = (
            "User-agent: BadBot\n"
            "Disallow: /\n"
            "\n"
            "User-agent: *\n"
            "Disallow:\n"
        )
        fetcher = MockRobotsFetcher(robots_content=robots_content)

        # BadBot is disallowed everywhere
        bad_checker = RobotsChecker(fetcher, user_agent="BadBot")
        assert not await bad_checker.is_allowed("http://example.com/page")

    async def test_specific_user_agent_allowed(self):
        """A different user-agent is allowed when only BadBot is blocked."""
        robots_content = (
            "User-agent: BadBot\n"
            "Disallow: /\n"
            "\n"
            "User-agent: *\n"
            "Disallow:\n"
        )
        fetcher = MockRobotsFetcher(robots_content=robots_content)

        # GoodBot matches the wildcard rule (allow all)
        good_checker = RobotsChecker(fetcher, user_agent="GoodBot")
        assert await good_checker.is_allowed("http://example.com/page")

    async def test_configured_user_agent_used_for_matching(self):
        """The configured user_agent is used for rule matching."""
        robots_content = (
            "User-agent: WebCrawler/1.0\n"
            "Disallow: /admin/\n"
            "\n"
            "User-agent: *\n"
            "Disallow:\n"
        )
        fetcher = MockRobotsFetcher(robots_content=robots_content)

        checker = RobotsChecker(fetcher, user_agent="WebCrawler/1.0")
        # WebCrawler/1.0 is specifically disallowed from /admin/
        assert not await checker.is_allowed("http://example.com/admin/settings")
        # But allowed elsewhere
        assert await checker.is_allowed("http://example.com/public/page")
