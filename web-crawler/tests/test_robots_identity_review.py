"""The robots request must identify the same crawler as its page requests."""

import pytest
from web_crawler.config import CrawlerConfig
from web_crawler.crawler import Crawler
from web_crawler.fetcher import Fetcher, FetchResponse
from web_crawler.models import CrawlStatus
from web_crawler.robots import RobotsChecker


class AgentPolicyFetcher(Fetcher):
    """An origin serves its bot policy only to the configured user agent."""

    def __init__(self, redirect):
        self.redirect = redirect
        self.requests = []

    async def fetch(self, url, headers=None, timeout=30):
        self.requests.append((url, dict(headers or {})))
        if self.redirect and url == "https://example.org/robots.txt":
            return FetchResponse(
                302, {"Location": "https://rules.example/robots.txt"}, b"", url
            )
        if url.endswith("/robots.txt"):
            body = (
                b"User-agent: AuditBot\nDisallow: /private"
                if (headers or {}).get("User-Agent") == "AuditBot/1.0"
                else b"User-agent: *\nDisallow:"
            )
            return FetchResponse(200, {"Content-Type": "text/plain"}, body, url)
        return FetchResponse(200, {"Content-Type": "text/html"}, b"<html></html>", url)


@pytest.mark.parametrize("redirect", [False, True])
async def test_configured_identity_prevents_forbidden_page_fetch(redirect):
    fetcher = AgentPolicyFetcher(redirect)
    crawler = Crawler(
        CrawlerConfig(max_pages=1, per_host_delay=0.0001, user_agent="AuditBot/1.0"),
        fetcher=fetcher,
    )
    results = await crawler.crawl(["https://example.org/private"])
    assert results[0].status is CrawlStatus.DISALLOWED
    assert [url for url, _ in fetcher.requests] == (
        ["https://example.org/robots.txt", "https://rules.example/robots.txt"]
        if redirect
        else ["https://example.org/robots.txt"]
    )
    assert all(
        headers.get("User-Agent") == "AuditBot/1.0" for _, headers in fetcher.requests
    )


async def test_robots_origin_ignores_credentials_and_default_port():
    fetcher = AgentPolicyFetcher(False)
    checker = RobotsChecker(fetcher, user_agent="AuditBot/1.0")
    assert not await checker.is_allowed("https://user@example.org/private")
    assert not await checker.is_allowed("https://example.org:443/private")
    assert [url for url, _ in fetcher.requests] == ["https://example.org/robots.txt"]
    assert not await checker.is_allowed("https://example.org:444/private")
    assert len(fetcher.requests) == 2
