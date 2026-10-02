"""Main Crawler orchestrator for the web crawler library.

Coordinates the crawl pipeline: frontier → robots check → fetch → parse →
deduplicate → extract links → filter → enqueue. Uses asyncio.Semaphore
to limit concurrent downloads.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Optional

from web_crawler.config import CrawlerConfig
from web_crawler.dedup import ContentSeen, URLSeen
from web_crawler.exceptions import FetchError
from web_crawler.fetcher import Fetcher, URLLibFetcher
from web_crawler.filters import URLFilter
from web_crawler.frontier import URLFrontier
from web_crawler.models import CrawlResult, CrawlStatus, FrontierEntry, Priority
from web_crawler.parser import ContentParser, LinkExtractor
from web_crawler.robots import RobotsChecker

logger = logging.getLogger(__name__)


class Crawler:
    """Main crawl loop orchestrator.

    Coordinates: frontier → robots check → fetch → parse →
    deduplicate → extract links → filter → enqueue.

    Uses asyncio.Semaphore to limit concurrent downloads.
    """

    def __init__(
        self,
        config: Optional[CrawlerConfig] = None,
        fetcher: Optional[Fetcher] = None,
    ) -> None:
        self._config = config or CrawlerConfig()
        self._fetcher = fetcher or URLLibFetcher()
        self._frontier = URLFrontier(per_host_delay=self._config.per_host_delay)
        self._url_seen = URLSeen()
        self._content_seen = ContentSeen()
        self._robots = RobotsChecker(
            self._fetcher,
            user_agent=self._config.user_agent,
            cache_ttl=self._config.robots_cache_ttl,
        )
        self._parser = ContentParser()
        self._link_extractor = LinkExtractor()
        self._url_filter = URLFilter(
            extension_blacklist=self._config.extension_blacklist,
            domain_whitelist=self._config.domain_whitelist,
            domain_blacklist=self._config.domain_blacklist,
            max_url_length=self._config.max_url_length,
            custom_filters=self._config.custom_filters,
        )
        self._semaphore = asyncio.Semaphore(self._config.max_concurrent)
        self._results: list[CrawlResult] = []
        self._pages_crawled = 0

    async def crawl(self, seed_urls: list[str]) -> list[CrawlResult]:
        """Run the crawl loop starting from seed URLs.

        Enqueues seed URLs with HIGH priority at depth 0, then enters
        the main loop: dequeue from frontier, create async tasks, wait
        for completion. Stops when max_pages is reached or frontier is empty.

        Args:
            seed_urls: Initial URLs to begin crawling from.

        Returns:
            List of CrawlResult for all processed URLs.
        """
        # Enqueue seed URLs
        for url in seed_urls:
            self._enqueue_url(url, priority=Priority.HIGH, depth=0)

        # Crawl loop
        tasks: set[asyncio.Task] = set()
        while not self._frontier.is_empty() or tasks:
            # Check stop condition
            if self._pages_crawled >= self._config.max_pages:
                break

            # Try to dequeue and start new downloads
            while (
                not self._frontier.is_empty()
                and self._pages_crawled + len(tasks) < self._config.max_pages
            ):
                entry = self._frontier.get()
                if entry is None:
                    break  # No URL ready (politeness delay)
                task = asyncio.create_task(self._process_url(entry))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

            if not tasks:
                # No tasks running and nothing ready from frontier
                # Wait briefly for politeness delays to expire
                await asyncio.sleep(0.1)
                continue

            # Wait for at least one task to complete
            done, tasks = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )

        # Cancel remaining tasks if we hit max_pages
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        return self._results

    async def _process_url(self, entry: FrontierEntry) -> None:
        """Process a single URL through the full pipeline.

        Pipeline steps:
        1. Check robots.txt
        2. Fetch the URL
        3. Content deduplication
        4. Parse and validate HTML
        5. Extract links
        6. Build success result and increment pages_crawled
        7. Invoke content hooks
        8. Filter and enqueue new links

        Args:
            entry: The frontier entry containing URL and metadata.
        """
        async with self._semaphore:
            url = entry.url
            depth = entry.depth

            # 1. Check robots.txt
            if not await self._robots.is_allowed(url):
                result = CrawlResult(
                    url=url, status=CrawlStatus.DISALLOWED, depth=depth
                )
                self._results.append(result)
                return

            # 2. Fetch
            try:
                response = await self._fetcher.fetch(
                    url,
                    headers={"User-Agent": self._config.user_agent},
                    timeout=self._config.request_timeout,
                )
            except FetchError as e:
                result = CrawlResult(
                    url=url,
                    status=CrawlStatus.ERROR,
                    depth=depth,
                    error_message=e.reason,
                )
                self._results.append(result)
                return

            # 3. Content deduplication
            fingerprint = self._content_seen.compute_fingerprint(response.body)
            if self._content_seen.is_seen(fingerprint):
                result = CrawlResult(
                    url=url,
                    status=CrawlStatus.DUPLICATE,
                    http_status=response.status_code,
                    content_fingerprint=fingerprint,
                    depth=depth,
                    headers=response.headers,
                )
                self._results.append(result)
                return
            self._content_seen.add(fingerprint)

            # 4. Parse and validate HTML
            content_type = response.headers.get("Content-Type", "")
            if not self._parser.is_html(content_type, response.body):
                result = CrawlResult(
                    url=url,
                    status=CrawlStatus.ERROR,
                    http_status=response.status_code,
                    depth=depth,
                    error_message="Non-HTML content",
                    headers=response.headers,
                )
                self._results.append(result)
                return

            # 5. Extract links
            html_text = response.body.decode("utf-8", errors="replace")
            links = self._link_extractor.extract(html_text, url)

            # 6. Build success result
            result = CrawlResult(
                url=url,
                status=CrawlStatus.SUCCESS,
                http_status=response.status_code,
                content_length=len(response.body),
                content_fingerprint=fingerprint,
                links=links,
                depth=depth,
                headers=response.headers,
            )
            self._results.append(result)
            self._pages_crawled += 1

            # 7. Invoke content hooks
            await self._invoke_hooks(result, html_text)

            # 8. Filter and enqueue new links
            for link in links:
                if self._url_filter.accept(link):
                    self._enqueue_url(link, Priority.MEDIUM, depth + 1)

    def _enqueue_url(self, url: str, priority: Priority, depth: int) -> None:
        """Enqueue a URL if not already seen and within max_depth.

        Checks max_depth enforcement and URL deduplication before
        adding to the frontier.

        Args:
            url: The URL to enqueue.
            priority: Crawl priority for this URL.
            depth: The depth level of this URL from seed.
        """
        if depth > self._config.max_depth:
            return
        if self._url_seen.is_seen(url):
            return
        self._url_seen.add(url)
        entry = FrontierEntry(url=url, priority=priority, depth=depth)
        self._frontier.put(entry)

    async def _invoke_hooks(self, result: CrawlResult, content: str) -> None:
        """Invoke all registered content hooks for a successful crawl.

        Supports both synchronous and asynchronous hook callables.
        Each hook is called with keyword arguments: url, status, headers,
        content, links. Exceptions from hooks are logged but do not
        interrupt the crawl (exception isolation).

        Args:
            result: The CrawlResult for the successfully crawled page.
            content: The decoded HTML content string.
        """
        for hook in self._config.content_hooks:
            try:
                if inspect.iscoroutinefunction(hook):
                    await hook(
                        url=result.url,
                        status=result.http_status,
                        headers=result.headers,
                        content=content,
                        links=result.links,
                    )
                else:
                    hook(
                        url=result.url,
                        status=result.http_status,
                        headers=result.headers,
                        content=content,
                        links=result.links,
                    )
            except Exception as e:
                logger.error(
                    f"Content hook error for {result.url}: {e}"
                )
