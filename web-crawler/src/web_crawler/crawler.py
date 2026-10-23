"""Main Crawler orchestrator for the web crawler library.

Coordinates the crawl pipeline: frontier → robots check → fetch → parse →
deduplicate → extract links → filter → enqueue. Uses asyncio.Semaphore to limit
concurrent downloads.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Optional

import web_crawler.config
import web_crawler.dedup
import web_crawler.exceptions
import web_crawler.fetcher
import web_crawler.filters
import web_crawler.frontier
import web_crawler.models
import web_crawler.parser
import web_crawler.robots

logger = logging.getLogger(__name__)


class Crawler:
    """Main crawl loop orchestrator.

    Coordinates: frontier → robots check → fetch → parse →
    deduplicate → extract links → filter → enqueue.

    Uses asyncio.Semaphore to limit concurrent downloads.
    """

    def __init__(
        self,
        config: Optional[web_crawler.config.CrawlerConfig] = None,
        fetcher: Optional[web_crawler.fetcher.Fetcher] = None,
    ) -> None:
        self._config = config or web_crawler.config.CrawlerConfig()
        self._fetcher = fetcher or web_crawler.fetcher.URLLibFetcher()
        self._frontier = web_crawler.frontier.URLFrontier(
            per_host_delay=self._config.per_host_delay
        )
        self._url_seen = web_crawler.dedup.URLSeen()
        self._content_seen = web_crawler.dedup.ContentSeen()
        self._robots = web_crawler.robots.RobotsChecker(
            self._fetcher,
            user_agent=self._config.user_agent,
            cache_ttl=self._config.robots_cache_ttl,
        )
        self._parser = web_crawler.parser.ContentParser()
        self._link_extractor = web_crawler.parser.LinkExtractor()
        self._url_filter = web_crawler.filters.URLFilter(
            extension_blacklist=self._config.extension_blacklist,
            domain_whitelist=self._config.domain_whitelist,
            domain_blacklist=self._config.domain_blacklist,
            max_url_length=self._config.max_url_length,
            custom_filters=self._config.custom_filters,
        )
        self._semaphore = asyncio.Semaphore(self._config.max_concurrent)
        self._results: list[web_crawler.models.CrawlResult] = []
        self._pages_crawled = 0

    async def crawl(self, seed_urls: list[str]) -> list[web_crawler.models.CrawlResult]:
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
            self._enqueue_url(url, priority=web_crawler.models.Priority.HIGH, depth=0)

        tasks: set[asyncio.Task] = set()
        try:
            while not self._frontier.is_empty() or tasks:
                if self._pages_crawled >= self._config.max_pages:
                    break
                while (
                    not self._frontier.is_empty()
                    and len(tasks) < self._config.max_concurrent
                    and self._pages_crawled + len(tasks) < self._config.max_pages
                ):
                    entry = self._frontier.get()
                    if entry is None:
                        break
                    tasks.add(asyncio.create_task(self._process_url(entry)))
                if not tasks:
                    await asyncio.sleep(0.1)
                    continue
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                # Keep all tasks owned until their exceptions have been observed.
                await asyncio.gather(*done)
                tasks = pending
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        return self._results

    async def _process_url(self, entry: web_crawler.models.FrontierEntry) -> None:
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
                result = web_crawler.models.CrawlResult(
                    url=url,
                    status=web_crawler.models.CrawlStatus.DISALLOWED,
                    depth=depth,
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
            except web_crawler.exceptions.FetchError as e:
                result = web_crawler.models.CrawlResult(
                    url=url,
                    status=web_crawler.models.CrawlStatus.ERROR,
                    depth=depth,
                    error_message=e.reason,
                )
                self._results.append(result)
                return

            # 3. Content deduplication
            fingerprint = self._content_seen.compute_fingerprint(response.body)
            if self._content_seen.is_seen(fingerprint):
                result = web_crawler.models.CrawlResult(
                    url=url,
                    status=web_crawler.models.CrawlStatus.DUPLICATE,
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
                result = web_crawler.models.CrawlResult(
                    url=url,
                    status=web_crawler.models.CrawlStatus.ERROR,
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
            result = web_crawler.models.CrawlResult(
                url=url,
                status=web_crawler.models.CrawlStatus.SUCCESS,
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
                    self._enqueue_url(
                        link, web_crawler.models.Priority.MEDIUM, depth + 1
                    )

    def _enqueue_url(
        self, url: str, priority: web_crawler.models.Priority, depth: int
    ) -> None:
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
        entry = web_crawler.models.FrontierEntry(
            url=url, priority=priority, depth=depth
        )
        self._frontier.put(entry)

    async def _invoke_hooks(
        self, result: web_crawler.models.CrawlResult, content: str
    ) -> None:
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
            # Isolate this operation; record failures before continuing.
            except Exception as e:  # pylint: disable=broad-exception-caught
                logging.getLogger(__name__).exception(
                    "Failure isolated in %s", "_invoke_hooks"
                )
                logger.error("Content hook error for %s: %s", result.url, e)
