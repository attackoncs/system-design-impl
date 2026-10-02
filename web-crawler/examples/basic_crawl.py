"""Basic crawl example using a mock fetcher for demonstration.

Usage:
    python -m examples.basic_crawl

Or from the web-crawler directory:
    PYTHONPATH=src python examples/basic_crawl.py
"""

import asyncio

from web_crawler import (
    Crawler,
    CrawlerConfig,
    Fetcher,
    FetchResponse,
    Priority,
)


class MockFetcher(Fetcher):
    """Mock fetcher with predefined page responses for demonstration."""

    def __init__(self) -> None:
        self._pages: dict[str, str] = {
            "http://example.com": '<html><body><a href="/about">About</a><a href="/blog">Blog</a></body></html>',
            "http://example.com/about": '<html><body><h1>About Us</h1><a href="/">Home</a></body></html>',
            "http://example.com/blog": '<html><body><h1>Blog</h1><a href="/blog/post1">Post 1</a></body></html>',
            "http://example.com/blog/post1": "<html><body><h1>Post 1</h1><p>Content here.</p></body></html>",
            "http://example.com/robots.txt": "User-agent: *\nAllow: /\n",
        }

    async def fetch(self, url, headers=None, timeout=30.0):
        if url in self._pages:
            body = self._pages[url].encode()
            ct = "text/plain" if url.endswith("robots.txt") else "text/html"
            return FetchResponse(
                status_code=200,
                headers={"Content-Type": ct},
                body=body,
                url=url,
            )
        from web_crawler.exceptions import FetchError
        raise FetchError(url, "Not found")


async def main() -> None:
    config = CrawlerConfig(
        max_pages=10,
        max_depth=2,
        max_concurrent=3,
        per_host_delay=0.1,
        domain_whitelist=["example.com"],
    )
    crawler = Crawler(config=config, fetcher=MockFetcher())
    results = await crawler.crawl(["http://example.com"])

    print(f"Crawled {len(results)} pages:")
    for r in results:
        print(f"  [{r.status.value}] {r.url} (depth={r.depth}, links={len(r.links)})")


if __name__ == "__main__":
    asyncio.run(main())
