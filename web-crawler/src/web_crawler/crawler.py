"""Main Crawler orchestrator for the web crawler library.

Coordinates the crawl pipeline: frontier → robots check → fetch → parse →
deduplicate → extract links → filter → enqueue. Uses asyncio.Semaphore to limit
concurrent downloads.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import urllib.parse
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
from web_crawler.state import SQLiteCrawlState

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
        *,
        partition=None,
        route_url=None,
    ) -> None:
        self._config = config or web_crawler.config.CrawlerConfig()
        self._partition, self._route_url = partition, route_url
        if partition is not None and route_url is None:
            raise ValueError(
                "partitioned crawling requires a route_url(owner, entry) callback"
            )
        self._fetcher = fetcher or web_crawler.fetcher.URLLibFetcher(
            max_response_bytes=self._config.max_response_bytes,
            max_redirects=self._config.max_redirects,
        )
        self._frontier = web_crawler.frontier.URLFrontier(
            per_host_delay=self._config.per_host_delay
        )
        self._state = None
        if self._config.state_path:
            self._state = SQLiteCrawlState(
                self._config.state_path,
                per_host_delay=self._config.per_host_delay,
                recrawl_interval=self._config.recrawl_interval_seconds,
                max_retries=self._config.max_retries,
                retry_delay=self._config.retry_delay_seconds,
            )
            self._frontier = self._state
        self._running = False
        self._content_lock = asyncio.Lock()
        self._content_pending = {}
        self._claims = {}
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

    async def _io(self, operation, *args, **kwargs):
        """Drain an already-started database operation before releasing ownership."""
        task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _queue_call(self, method, *args, **kwargs):
        operation = getattr(self._frontier, method)
        if self._state is not None:
            return await self._io(operation, *args, **kwargs)
        return operation(*args, **kwargs)

    async def crawl(self, seed_urls: list[str]) -> list[web_crawler.models.CrawlResult]:
        """Run one finite batch; durable due recrawls are promoted on each run.

        max_pages bounds attempts in this batch, including errors/duplicates.
        Pending work remains available for a later run. Concurrent calls on one
        crawler are rejected; state files also reject concurrent owners.
        """
        if self._running:
            raise RuntimeError("crawler is already running")
        self._running = True
        tasks = set()
        self._results, self._pages_crawled, self._attempted = [], 0, 0
        # Completed fingerprints live on disk in durable mode; transient claims
        # are only shared by tasks in this batch.
        if self._state is not None:
            self._content_seen = web_crawler.dedup.ContentSeen()
        try:
            if self._state is not None:
                await self._io(self._state.open)
            for url in seed_urls:
                await self._enqueue_url(url, web_crawler.models.Priority.HIGH, 0)
            while not await self._queue_call("is_empty") or tasks:
                if self._attempted >= self._config.max_pages:
                    break
                while (
                    len(tasks) < self._config.max_concurrent
                    and self._attempted + len(tasks) < self._config.max_pages
                ):
                    entry = await self._queue_call("get", reserve=True)
                    if entry is None:
                        break
                    tasks.add(asyncio.create_task(self._process_url(entry)))
                if not tasks:
                    await asyncio.sleep(0.05)
                    continue
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                await asyncio.gather(*done)
                tasks = pending
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if self._state is not None and self._state._lock_file is not None:
                await self._io(self._state.close)
            self._running = False
        return list(self._results)

    async def _process_url(self, entry):
        acknowledged, result = False, None
        try:
            try:
                result, html = await self._process_entry(entry)
            except Exception as error:
                logger.exception("URL processing failed: %s", entry.url)
                result = web_crawler.models.CrawlResult(
                    entry.url,
                    web_crawler.models.CrawlStatus.ERROR,
                    depth=entry.depth,
                    error_message=str(error),
                )
                html = None
            if self._state is not None:
                await self._io(self._state.finish, entry, result, html)
            else:
                self._frontier.complete(entry)
            acknowledged = True
            self._results.append(result)
            self._attempted += 1
            if result.status == web_crawler.models.CrawlStatus.SUCCESS:
                self._pages_crawled += 1
        finally:
            self._release_content(
                entry.url,
                acknowledged
                and result.status == web_crawler.models.CrawlStatus.SUCCESS,
            )
            if not acknowledged:
                if self._state is not None:
                    await self._io(self._state.retry, entry)
                else:
                    self._frontier.complete(entry)
                    self._frontier.put(entry)

    async def _process_entry(self, entry):
        status = web_crawler.models.CrawlStatus
        url, depth = entry.url, entry.depth
        if self._partition is not None and not self._partition.owns(url):
            await self._route(entry)
            return web_crawler.models.CrawlResult(
                url, status.FILTERED, depth=depth
            ), None
        async with self._semaphore:
            if not await asyncio.wait_for(
                self._robots.is_allowed(url), self._config.request_timeout
            ):
                return web_crawler.models.CrawlResult(
                    url, status.DISALLOWED, depth=depth
                ), None
            # The robots download is itself a request. Reserve the host while
            # spacing it from the page fetch, and respect advertised Crawl-delay.
            await asyncio.sleep(
                max(self._config.per_host_delay, self._robots.crawl_delay(url))
            )
            try:
                response = await asyncio.wait_for(
                    self._fetcher.fetch(
                        url,
                        headers={"User-Agent": self._config.user_agent},
                        timeout=self._config.request_timeout,
                    ),
                    self._config.request_timeout,
                )
            except web_crawler.exceptions.FetchError as error:
                return web_crawler.models.CrawlResult(
                    url, status.ERROR, depth=depth, error_message=error.reason
                ), None
            headers = {name.lower(): value for name, value in response.headers.items()}
            # Cross-origin redirects re-enter routing, filtering and robots checks.
            if 300 <= response.status_code < 400:
                location = headers.get("location")
                if location:
                    await self._enqueue_url(
                        urllib.parse.urljoin(response.url, location),
                        entry.priority,
                        depth + 1,
                    )
                return web_crawler.models.CrawlResult(
                    url,
                    status.FILTERED,
                    http_status=response.status_code,
                    depth=depth,
                    headers=response.headers,
                ), None
            if not 200 <= response.status_code < 300:
                return web_crawler.models.CrawlResult(
                    url,
                    status.ERROR,
                    http_status=response.status_code,
                    depth=depth,
                    error_message=f"HTTP {response.status_code}",
                ), None
            if not self._parser.is_html(headers.get("content-type", ""), response.body):
                return web_crawler.models.CrawlResult(
                    url,
                    status.ERROR,
                    http_status=response.status_code,
                    depth=depth,
                    error_message="Non-HTML content",
                ), None
            fingerprint = self._content_seen.compute_fingerprint(response.body)
            duplicate = await self._claim_content(fingerprint, url)
            html = response.body.decode("utf-8", errors="replace")
            links = self._link_extractor.extract(html, response.url)
            if duplicate:
                # Identical bytes at different base URLs can have distinct
                # relative link targets. Deduplicate storage, not discovery.
                for link in links:
                    await self._enqueue_url(
                        link, web_crawler.models.Priority.MEDIUM, depth + 1
                    )
                return web_crawler.models.CrawlResult(
                    url,
                    status.DUPLICATE,
                    http_status=response.status_code,
                    content_fingerprint=fingerprint,
                    depth=depth,
                    links=links,
                ), None
            result = web_crawler.models.CrawlResult(
                url,
                status.SUCCESS,
                http_status=response.status_code,
                content_length=len(response.body),
                content_fingerprint=fingerprint,
                links=links,
                depth=depth,
                headers=response.headers,
            )
            await self._invoke_hooks(result, html)
            for link in links:
                await self._enqueue_url(
                    link, web_crawler.models.Priority.MEDIUM, depth + 1
                )
            return result, html

    async def _claim_content(self, fingerprint, url):
        while True:
            async with self._content_lock:
                if self._content_seen.is_seen(fingerprint):
                    return True
                pending = self._content_pending.get(fingerprint)
                if pending is None:
                    if self._state is not None and await self._io(
                        self._state.content_seen, fingerprint
                    ):
                        self._content_seen.add(fingerprint)
                        return True
                    pending = asyncio.get_running_loop().create_future()
                    self._content_pending[fingerprint] = pending
                    self._claims[url] = fingerprint
                    return False
            # A failed/cancelled owner releases the claim; retry the lookup.
            await asyncio.shield(pending)

    def _release_content(self, url, committed):
        fingerprint = self._claims.pop(url, None)
        if fingerprint is not None:
            if committed:
                self._content_seen.add(fingerprint)
            pending = self._content_pending.pop(fingerprint)
            pending.set_result(None)

    async def _route(self, entry):
        result = self._route_url(self._partition.owner_for_url(entry.url), entry)
        if inspect.isawaitable(result):
            await result

    async def _enqueue_url(self, url, priority, depth):
        if depth > self._config.max_depth or not self._url_filter.accept(url):
            return
        entry = web_crawler.models.FrontierEntry(url, priority, depth)
        if self._state is not None:
            # Do not route here: persist remote discoveries before handing them
            # to another worker, so a crash cannot lose the handoff.
            await self._queue_call("put", entry)
        elif not self._url_seen.is_seen(url):
            self._url_seen.add(url)
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
                outcome = hook(
                    url=result.url,
                    status=result.http_status,
                    headers=result.headers,
                    content=content,
                    links=result.links,
                )
                if inspect.isawaitable(outcome):
                    await outcome
            # Isolate this operation; record failures before continuing.
            except Exception as e:  # pylint: disable=broad-exception-caught
                logging.getLogger(__name__).exception(
                    "Failure isolated in %s", "_invoke_hooks"
                )
                logger.error("Content hook error for %s: %s", result.url, e)
