"""Unit tests for Crawler orchestrator."""

import web_crawler.config
import web_crawler.crawler
import web_crawler.exceptions
import web_crawler.fetcher
import web_crawler.models


class MockFetcher(web_crawler.fetcher.Fetcher):
    """Mock fetcher with predefined page responses."""

    def __init__(self, pages: dict[str, str]):
        """Args:
        pages: Mapping of URL -> HTML content string.
        """
        self._pages = pages

    async def fetch(self, url, headers=None, timeout=30.0):
        if url.endswith("/robots.txt"):
            return web_crawler.fetcher.FetchResponse(
                status_code=200,
                headers={"Content-Type": "text/plain"},
                body=b"User-agent: *\nAllow: /",
                url=url,
            )
        if url in self._pages:
            body = self._pages[url].encode("utf-8")
            return web_crawler.fetcher.FetchResponse(
                status_code=200,
                headers={"Content-Type": "text/html"},
                body=body,
                url=url,
            )
        raise web_crawler.exceptions.FetchError(url, "Not found")


class TestCrawlerFullCrawl:
    """Test full crawl with mock fetcher (seed URL → discover links → crawl
    them).
    """

    async def test_crawls_seed_and_discovered_links(self):
        pages = {
            "http://example.com/": (
                '<html><body><a href="/page1">P1</a></body></html>'
            ),
            "http://example.com/page1": "<html><body>Page 1</body></html>",
        }
        config = web_crawler.config.CrawlerConfig(
            max_pages=10,
            max_depth=3,
            per_host_delay=0.01,
            domain_whitelist=["example.com"],
        )
        crawler = web_crawler.crawler.Crawler(config=config, fetcher=MockFetcher(pages))
        results = await crawler.crawl(["http://example.com/"])

        urls = [r.url for r in results if r.status == web_crawler.models.CrawlStatus.SUCCESS]
        assert "http://example.com/" in urls
        assert "http://example.com/page1" in urls


class TestCrawlerMaxPages:
    """Test max_pages stops crawling."""

    async def test_stops_at_max_pages(self):
        pages = {
            "http://a.com/": (
                '<html><body><a href="http://b.com/">B</a></body></html>'
            ),
            "http://b.com/": (
                '<html><body><a href="http://c.com/">C</a></body></html>'
            ),
            "http://c.com/": "<html><body>C</body></html>",
            "http://a.com/robots.txt": "",
            "http://b.com/robots.txt": "",
            "http://c.com/robots.txt": "",
        }
        config = web_crawler.config.CrawlerConfig(max_pages=1, max_depth=5, per_host_delay=0.01)
        fetcher = MockFetcher(pages)
        crawler = web_crawler.crawler.Crawler(config=config, fetcher=fetcher)
        results = await crawler.crawl(["http://a.com/"])

        success_results = [
            r for r in results if r.status == web_crawler.models.CrawlStatus.SUCCESS
        ]
        assert len(success_results) <= 1


class TestCrawlerMaxDepth:
    """Test max_depth limits link following."""

    async def test_does_not_follow_beyond_max_depth(self):
        pages = {
            "http://a.com/": (
                '<html><body><a href="http://b.com/">B</a></body></html>'
            ),
            "http://b.com/": (
                '<html><body><a href="http://c.com/">C</a></body></html>'
            ),
            "http://c.com/": "<html><body>C</body></html>",
        }
        config = web_crawler.config.CrawlerConfig(
            max_pages=100,
            max_depth=1,  # Only follow 1 level deep
            per_host_delay=0.01,
        )
        crawler = web_crawler.crawler.Crawler(config=config, fetcher=MockFetcher(pages))
        results = await crawler.crawl(["http://a.com/"])

        urls = [r.url for r in results if r.status == web_crawler.models.CrawlStatus.SUCCESS]
        assert "http://a.com/" in urls
        assert "http://b.com/" in urls
        # c.com is at depth 2, should not be crawled
        assert "http://c.com/" not in urls


class TestCrawlerContentDedup:
    """Test content deduplication (same content at different URLs)."""

    async def test_duplicate_content_detected(self):
        same_content = "<html><body>Same content</body></html>"
        pages = {
            "http://a.com/page1": same_content,
            "http://a.com/page2": same_content,
        }
        config = web_crawler.config.CrawlerConfig(
            max_pages=10,
            max_depth=1,
            per_host_delay=0.01,
            domain_whitelist=["a.com"],
        )
        crawler = web_crawler.crawler.Crawler(config=config, fetcher=MockFetcher(pages))
        results = await crawler.crawl(
            ["http://a.com/page1", "http://a.com/page2"]
        )

        statuses = [r.status for r in results]
        assert web_crawler.models.CrawlStatus.SUCCESS in statuses
        assert web_crawler.models.CrawlStatus.DUPLICATE in statuses


class TestCrawlerRobotsDisallowed:
    """Test robots.txt disallowed URL."""

    async def test_disallowed_url_produces_disallowed_result(self):
        class DisallowFetcher(web_crawler.fetcher.Fetcher):
            async def fetch(self, url, headers=None, timeout=30.0):
                if url.endswith("/robots.txt"):
                    return web_crawler.fetcher.FetchResponse(
                        status_code=200,
                        headers={"Content-Type": "text/plain"},
                        body=b"User-agent: *\nDisallow: /",
                        url=url,
                    )
                return web_crawler.fetcher.FetchResponse(
                    status_code=200,
                    headers={"Content-Type": "text/html"},
                    body=b"<html></html>",
                    url=url,
                )

        config = web_crawler.config.CrawlerConfig(max_pages=10, per_host_delay=0.01)
        crawler = web_crawler.crawler.Crawler(config=config, fetcher=DisallowFetcher())
        results = await crawler.crawl(["http://example.com/page"])

        assert len(results) == 1
        assert results[0].status == web_crawler.models.CrawlStatus.DISALLOWED


class TestCrawlerContentHooks:
    """Test content hooks are invoked."""

    async def test_hook_invoked_on_success(self):
        pages = {
            "http://example.com/": "<html><body>Hello</body></html>",
        }
        hook_calls = []

        def my_hook(url, status, headers, content, links):
            hook_calls.append(url)

        config = web_crawler.config.CrawlerConfig(
            max_pages=10,
            per_host_delay=0.01,
            domain_whitelist=["example.com"],
            content_hooks=[my_hook],
        )
        crawler = web_crawler.crawler.Crawler(config=config, fetcher=MockFetcher(pages))
        await crawler.crawl(["http://example.com/"])

        assert "http://example.com/" in hook_calls


class TestCrawlerFetchError:
    """Test fetch error produces ERROR result."""

    async def test_fetch_error_produces_error_result(self):
        # MockFetcher raises FetchError for unknown URLs
        pages = {}  # No pages defined
        config = web_crawler.config.CrawlerConfig(max_pages=10, per_host_delay=0.01)
        crawler = web_crawler.crawler.Crawler(config=config, fetcher=MockFetcher(pages))
        results = await crawler.crawl(["http://example.com/missing"])

        error_results = [r for r in results if r.status == web_crawler.models.CrawlStatus.ERROR]
        assert len(error_results) == 1
        assert error_results[0].error_message is not None
