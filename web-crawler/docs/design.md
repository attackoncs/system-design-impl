# Design: Web Crawler

## Architecture Overview

The web crawler library follows a pipeline architecture with pluggable components at each stage. The main orchestrator (`Crawler`) coordinates the crawl loop: dequeue URL → check robots → fetch → parse → deduplicate → extract links → filter → enqueue. Each stage is backed by an abstract interface, enabling custom implementations for testing and extensibility.

```
┌─────────────────────────────────────────────────────────────────┐
│                    Public API (Crawler)                          │
│              async crawl loop orchestrator                       │
├─────────────────────────────────────────────────────────────────┤
│  URL Frontier          │  Fetcher           │  Content Hooks     │
│  (priority + polite)   │  (ABC + urllib)    │  (async callables) │
├────────────────────────┼────────────────────┼────────────────────┤
│  Robots Checker        │  Content Parser    │  Link Extractor    │
│  (cached per-host)     │  (HTML validation) │  (href extraction) │
├────────────────────────┼────────────────────┼────────────────────┤
│  URL Filter            │  Content Dedup     │  URL Dedup         │
│  (configurable rules)  │  (SHA-256 set)     │  (normalized set)  │
├─────────────────────────────────────────────────────────────────┤
│  Models & Config & Exceptions                                    │
│  (CrawlResult, CrawlStatus, CrawlerConfig, Priority, etc.)      │
└─────────────────────────────────────────────────────────────────┘
```

## Project Structure

```
web-crawler/
├── pyproject.toml
├── README.md
├── docs/
│   ├── requirements.md
│   ├── design.md
│   └── tasks.md
├── src/
│   └── web_crawler/
│       ├── __init__.py          # Public API exports
│       ├── config.py            # CrawlerConfig dataclass
│       ├── models.py            # Data models (CrawlResult, CrawlStatus, Priority, FrontierEntry)
│       ├── exceptions.py        # Custom exception hierarchy
│       ├── frontier.py          # URLFrontier with front queues + back queues
│       ├── fetcher.py           # Fetcher ABC + default urllib implementation
│       ├── robots.py            # RobotsChecker with per-host caching
│       ├── parser.py            # ContentParser + LinkExtractor
│       ├── filters.py           # URLFilter with configurable rules
│       ├── dedup.py             # ContentSeen (SHA-256) + URLSeen (set-based)
│       └── crawler.py           # Main Crawler orchestrator (async crawl loop)
├── tests/
│   ├── __init__.py
│   ├── test_config.py
│   ├── test_models.py
│   ├── test_frontier.py
│   ├── test_fetcher.py
│   ├── test_robots.py
│   ├── test_parser.py
│   ├── test_filters.py
│   ├── test_dedup.py
│   ├── test_crawler.py
│   └── test_properties.py      # Property-based tests (Hypothesis)
└── examples/
    └── basic_crawl.py           # Simple crawl example with mock fetcher
```

## Component Design

### 1. Custom Exceptions (`exceptions.py`)

```python
class CrawlerError(Exception):
    """Base exception for all web crawler errors."""
    pass


class ConfigurationError(CrawlerError):
    """Raised when crawler configuration is invalid."""
    pass


class FetchError(CrawlerError):
    """Raised when an HTTP fetch operation fails."""

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"Failed to fetch '{url}': {reason}")


class RobotsDisallowedError(CrawlerError):
    """Raised when a URL is disallowed by robots.txt."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"URL disallowed by robots.txt: '{url}'")


class ContentParseError(CrawlerError):
    """Raised when HTML content cannot be parsed."""

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"Failed to parse content from '{url}': {reason}")
```

### 2. Data Models (`models.py`)

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class CrawlStatus(Enum):
    """Status of a single URL crawl attempt."""
    SUCCESS = "success"
    ERROR = "error"
    DUPLICATE = "duplicate"
    DISALLOWED = "disallowed"
    FILTERED = "filtered"


class Priority(Enum):
    """URL crawl priority levels for the front queue."""
    HIGH = 0
    MEDIUM = 1
    LOW = 2


@dataclass
class FrontierEntry:
    """An entry in the URL frontier with metadata."""
    url: str
    priority: Priority = Priority.MEDIUM
    depth: int = 0
    host: str = ""

    def __post_init__(self) -> None:
        if not self.host:
            from urllib.parse import urlparse
            self.host = urlparse(self.url).netloc.lower()

    def __lt__(self, other: "FrontierEntry") -> bool:
        """Compare by priority value for heapq ordering."""
        return self.priority.value < other.priority.value


@dataclass
class CrawlResult:
    """Result of crawling a single URL."""
    url: str
    status: CrawlStatus
    http_status: Optional[int] = None
    content_length: Optional[int] = None
    content_fingerprint: Optional[str] = None
    links: list[str] = field(default_factory=list)
    depth: int = 0
    error_message: Optional[str] = None
    headers: dict[str, str] = field(default_factory=dict)
```

### 3. Configuration (`config.py`)

```python
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class CrawlerConfig:
    """Configuration for the web crawler.

    All numeric values must be positive. Raises ValueError on invalid input.
    """
    max_pages: int = 100
    max_depth: int = 3
    max_concurrent: int = 10
    per_host_delay: float = 1.0
    request_timeout: float = 30.0
    user_agent: str = "WebCrawler/1.0"
    robots_cache_ttl: float = 3600.0

    # Filter configuration
    extension_blacklist: list[str] = field(default_factory=lambda: [
        ".jpg", ".jpeg", ".png", ".gif", ".svg",
        ".pdf", ".zip", ".exe", ".mp3", ".mp4", ".avi",
    ])
    domain_whitelist: Optional[list[str]] = None
    domain_blacklist: Optional[list[str]] = None
    max_url_length: int = 2048
    custom_filters: list[Callable[[str], bool]] = field(default_factory=list)

    # Hook configuration
    content_hooks: list[Callable] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Validate configuration values."""
        if self.max_pages <= 0:
            raise ValueError(f"max_pages must be positive, got {self.max_pages}")
        if self.max_depth <= 0:
            raise ValueError(f"max_depth must be positive, got {self.max_depth}")
        if self.max_concurrent <= 0:
            raise ValueError(f"max_concurrent must be positive, got {self.max_concurrent}")
        if self.per_host_delay <= 0:
            raise ValueError(f"per_host_delay must be positive, got {self.per_host_delay}")
        if self.request_timeout <= 0:
            raise ValueError(f"request_timeout must be positive, got {self.request_timeout}")
        if self.max_url_length <= 0:
            raise ValueError(f"max_url_length must be positive, got {self.max_url_length}")
        if self.robots_cache_ttl <= 0:
            raise ValueError(f"robots_cache_ttl must be positive, got {self.robots_cache_ttl}")
```

### 4. URL Frontier (`frontier.py`)

```python
import heapq
import time
from collections import deque
from typing import Optional

from web_crawler.models import FrontierEntry, Priority


class URLFrontier:
    """URL Frontier with priority front queues and politeness back queues.

    Front queues: A priority heap (heapq) that determines crawl order.
    Higher priority URLs (lower enum value) are dequeued first.

    Back queues: A dict mapping host -> deque of FrontierEntry.
    Enforces per-host delay between consecutive requests.
    """

    def __init__(self, per_host_delay: float = 1.0) -> None:
        self._per_host_delay = per_host_delay
        # Front queue: min-heap ordered by priority
        self._front_queue: list[tuple[int, int, FrontierEntry]] = []
        self._counter = 0  # Tie-breaker for heap stability
        # Back queues: host -> deque of entries waiting for politeness
        self._back_queues: dict[str, deque[FrontierEntry]] = {}
        # Per-host last access timestamps
        self._host_last_access: dict[str, float] = {}
        self._size = 0

    def put(self, entry: FrontierEntry) -> None:
        """Add a URL to the frontier.

        Assigns to front queue by priority and back queue by host.
        """
        # Add to back queue for the host
        if entry.host not in self._back_queues:
            self._back_queues[entry.host] = deque()
        self._back_queues[entry.host].append(entry)

        # Add to front queue (priority heap)
        heapq.heappush(
            self._front_queue,
            (entry.priority.value, self._counter, entry),
        )
        self._counter += 1
        self._size += 1

    def get(self) -> Optional[FrontierEntry]:
        """Dequeue the next URL respecting priority and politeness.

        Returns the highest-priority URL whose host's politeness delay
        has elapsed. Returns None if no URL is currently available.
        """
        now = time.monotonic()
        skipped: list[tuple[int, int, FrontierEntry]] = []

        result: Optional[FrontierEntry] = None
        while self._front_queue:
            priority_val, counter, entry = heapq.heappop(self._front_queue)

            # Check politeness: has enough time passed for this host?
            last_access = self._host_last_access.get(entry.host, 0.0)
            if now - last_access >= self._per_host_delay:
                # This entry is ready
                result = entry
                self._host_last_access[entry.host] = now
                # Remove from back queue
                if entry.host in self._back_queues:
                    back_q = self._back_queues[entry.host]
                    if back_q and back_q[0] is entry:
                        back_q.popleft()
                    if not back_q:
                        del self._back_queues[entry.host]
                self._size -= 1
                break
            else:
                # Not ready yet, skip for now
                skipped.append((priority_val, counter, entry))

        # Put skipped entries back
        for item in skipped:
            heapq.heappush(self._front_queue, item)

        return result

    def is_empty(self) -> bool:
        """Check if the frontier has no pending URLs."""
        return self._size == 0

    @property
    def size(self) -> int:
        """Number of pending URLs in the frontier."""
        return self._size
```

### 5. Fetcher Interface (`fetcher.py`)

```python
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


@dataclass
class FetchResponse:
    """Response from an HTTP fetch operation."""
    status_code: int
    headers: dict[str, str]
    body: bytes
    url: str


class Fetcher(ABC):
    """Abstract interface for HTTP fetching."""

    @abstractmethod
    async def fetch(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 30.0,
    ) -> FetchResponse:
        """Fetch a URL and return the response.

        Args:
            url: The URL to fetch.
            headers: Optional request headers.
            timeout: Request timeout in seconds.

        Returns:
            FetchResponse with status, headers, and body.

        Raises:
            FetchError: If the fetch fails.
        """
        ...


class URLLibFetcher(Fetcher):
    """Default fetcher using urllib.request wrapped in asyncio executor.

    Wraps blocking urllib calls in run_in_executor for async compatibility.
    """

    async def fetch(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 30.0,
    ) -> FetchResponse:
        """Fetch URL using urllib in a thread executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._blocking_fetch, url, headers or {}, timeout
        )

    def _blocking_fetch(
        self,
        url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> FetchResponse:
        """Synchronous fetch using urllib.request."""
        from web_crawler.exceptions import FetchError

        req = Request(url, headers=headers)
        try:
            response = urlopen(req, timeout=timeout)
            resp_headers = {k: v for k, v in response.getheaders()}
            body = response.read()
            return FetchResponse(
                status_code=response.status,
                headers=resp_headers,
                body=body,
                url=url,
            )
        except HTTPError as e:
            raise FetchError(url, f"HTTP {e.code}: {e.reason}")
        except URLError as e:
            raise FetchError(url, str(e.reason))
        except TimeoutError:
            raise FetchError(url, "Request timed out")
```

### 6. Robots Checker (`robots.py`)

```python
import time
from typing import Optional
from urllib.robotparser import RobotFileParser

from web_crawler.fetcher import Fetcher


class RobotsChecker:
    """Checks robots.txt compliance with per-host caching.

    Fetches and parses robots.txt for each host on first access,
    caches the result for the configured TTL period.
    """

    def __init__(
        self,
        fetcher: Fetcher,
        user_agent: str = "WebCrawler/1.0",
        cache_ttl: float = 3600.0,
    ) -> None:
        self._fetcher = fetcher
        self._user_agent = user_agent
        self._cache_ttl = cache_ttl
        # host -> (RobotFileParser, timestamp)
        self._cache: dict[str, tuple[RobotFileParser, float]] = {}

    async def is_allowed(self, url: str) -> bool:
        """Check if a URL is allowed by robots.txt.

        Args:
            url: The URL to check.

        Returns:
            True if allowed, False if disallowed.
        """
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = parsed.netloc.lower()
        robots_url = f"{parsed.scheme}://{host}/robots.txt"

        parser = await self._get_parser(host, robots_url)
        if parser is None:
            return True  # If robots.txt unavailable, allow all
        return parser.can_fetch(self._user_agent, url)

    async def _get_parser(
        self, host: str, robots_url: str
    ) -> Optional[RobotFileParser]:
        """Get or fetch the robots.txt parser for a host."""
        now = time.monotonic()

        # Check cache
        if host in self._cache:
            parser, cached_at = self._cache[host]
            if now - cached_at < self._cache_ttl:
                return parser

        # Fetch robots.txt
        try:
            response = await self._fetcher.fetch(robots_url, timeout=10.0)
            parser = RobotFileParser()
            parser.parse(response.body.decode("utf-8", errors="replace").splitlines())
            self._cache[host] = (parser, now)
            return parser
        except Exception:
            # If robots.txt cannot be fetched, allow all
            self._cache[host] = (None, now)  # type: ignore
            return None
```

### 7. Content Parser and Link Extractor (`parser.py`)

```python
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse


class LinkExtractorParser(HTMLParser):
    """HTMLParser subclass that extracts href attributes from <a> tags."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value is not None:
                    self.links.append(value)


def normalize_url(url: str) -> str:
    """Normalize a URL by lowercasing scheme/host and removing fragments.

    Args:
        url: The URL to normalize.

    Returns:
        Normalized URL string.
    """
    parsed = urlparse(url)
    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        fragment="",
    )
    return urlunparse(normalized)


class ContentParser:
    """Validates that content is HTML."""

    def is_html(self, content_type: str, body: bytes) -> bool:
        """Check if content is HTML based on Content-Type header.

        Args:
            content_type: The Content-Type header value.
            body: The response body bytes.

        Returns:
            True if content appears to be HTML.
        """
        if content_type:
            return "text/html" in content_type.lower()
        # Content sniffing fallback: check for HTML markers
        snippet = body[:512].lower()
        return b"<html" in snippet or b"<!doctype" in snippet


class LinkExtractor:
    """Extracts and normalizes links from HTML content."""

    def extract(self, html: str, base_url: str) -> list[str]:
        """Extract all links from HTML, resolving relative URLs.

        Args:
            html: The HTML content string.
            base_url: The base URL for resolving relative links.

        Returns:
            List of absolute, normalized URL strings.
        """
        parser = LinkExtractorParser()
        parser.feed(html)

        results: list[str] = []
        for href in parser.links:
            # Resolve relative URLs
            absolute = urljoin(base_url, href)
            # Normalize
            normalized = normalize_url(absolute)
            if normalized:
                results.append(normalized)
        return results
```

### 8. URL Filter (`filters.py`)

```python
from typing import Callable, Optional
from urllib.parse import urlparse


class URLFilter:
    """Configurable URL filter with multiple rule types.

    Applies rules in order:
    1. Scheme check (http/https only)
    2. URL length check
    3. Domain whitelist (if configured)
    4. Domain blacklist (if configured)
    5. Extension blacklist
    6. Custom filter functions
    """

    def __init__(
        self,
        extension_blacklist: Optional[list[str]] = None,
        domain_whitelist: Optional[list[str]] = None,
        domain_blacklist: Optional[list[str]] = None,
        max_url_length: int = 2048,
        custom_filters: Optional[list[Callable[[str], bool]]] = None,
    ) -> None:
        self._extension_blacklist = set(
            ext.lower() for ext in (extension_blacklist or [])
        )
        self._domain_whitelist = (
            set(d.lower() for d in domain_whitelist) if domain_whitelist else None
        )
        self._domain_blacklist = set(
            d.lower() for d in (domain_blacklist or [])
        )
        self._max_url_length = max_url_length
        self._custom_filters = custom_filters or []

    def accept(self, url: str) -> bool:
        """Check if a URL passes all filter rules.

        Args:
            url: The URL to check.

        Returns:
            True if the URL is accepted, False if rejected.
        """
        parsed = urlparse(url)

        # 1. Scheme check
        if parsed.scheme.lower() not in ("http", "https"):
            return False

        # 2. Length check
        if len(url) > self._max_url_length:
            return False

        # 3. Domain whitelist (must be in whitelist if configured)
        domain = parsed.netloc.lower()
        if self._domain_whitelist is not None:
            if domain not in self._domain_whitelist:
                return False

        # 4. Domain blacklist
        if domain in self._domain_blacklist:
            return False

        # 5. Extension blacklist
        path_lower = parsed.path.lower()
        for ext in self._extension_blacklist:
            if path_lower.endswith(ext):
                return False

        # 6. Custom filters
        for filter_fn in self._custom_filters:
            if not filter_fn(url):
                return False

        return True
```

### 9. Deduplication (`dedup.py`)

```python
import hashlib

from web_crawler.parser import normalize_url


class ContentSeen:
    """Content deduplication using SHA-256 fingerprints.

    Maintains an in-memory set of content hashes to detect
    duplicate page content.
    """

    def __init__(self) -> None:
        self._fingerprints: set[str] = set()

    def compute_fingerprint(self, content: bytes) -> str:
        """Compute SHA-256 fingerprint of content.

        Args:
            content: The page body bytes.

        Returns:
            Hex digest string of the SHA-256 hash.
        """
        return hashlib.sha256(content).hexdigest()

    def is_seen(self, fingerprint: str) -> bool:
        """Check if a content fingerprint has been seen before.

        Args:
            fingerprint: SHA-256 hex digest.

        Returns:
            True if this content has been seen before.
        """
        return fingerprint in self._fingerprints

    def add(self, fingerprint: str) -> None:
        """Mark a content fingerprint as seen.

        Args:
            fingerprint: SHA-256 hex digest to record.
        """
        self._fingerprints.add(fingerprint)


class URLSeen:
    """URL deduplication using normalized URL set.

    Normalizes URLs (lowercase scheme/host, remove fragments)
    before comparison to avoid visiting equivalent URLs.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def is_seen(self, url: str) -> bool:
        """Check if a URL has been seen (after normalization).

        Args:
            url: The URL to check.

        Returns:
            True if this URL (or a normalized equivalent) has been seen.
        """
        return normalize_url(url) in self._seen

    def add(self, url: str) -> None:
        """Mark a URL as seen (stores normalized form).

        Args:
            url: The URL to record.
        """
        self._seen.add(normalize_url(url))

    @property
    def count(self) -> int:
        """Number of unique URLs seen."""
        return len(self._seen)
```

### 10. Crawler Orchestrator (`crawler.py`)

```python
import asyncio
import inspect
import logging
from typing import Callable, Optional

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
        """Process a single URL through the full pipeline."""
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
            if depth < self._config.max_depth:
                for link in links:
                    if self._url_filter.accept(link):
                        self._enqueue_url(link, Priority.MEDIUM, depth + 1)

    def _enqueue_url(self, url: str, priority: Priority, depth: int) -> None:
        """Enqueue a URL if not already seen."""
        if self._url_seen.is_seen(url):
            return
        self._url_seen.add(url)
        entry = FrontierEntry(url=url, priority=priority, depth=depth)
        self._frontier.put(entry)

    async def _invoke_hooks(self, result: CrawlResult, content: str) -> None:
        """Invoke all registered content hooks for a successful crawl."""
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
```

### 11. Public API (`__init__.py`)

```python
"""Web Crawler - An asyncio-based web crawling library."""

from web_crawler.config import CrawlerConfig
from web_crawler.crawler import Crawler
from web_crawler.dedup import ContentSeen, URLSeen
from web_crawler.exceptions import (
    ConfigurationError,
    ContentParseError,
    CrawlerError,
    FetchError,
    RobotsDisallowedError,
)
from web_crawler.fetcher import Fetcher, FetchResponse, URLLibFetcher
from web_crawler.filters import URLFilter
from web_crawler.frontier import URLFrontier
from web_crawler.models import CrawlResult, CrawlStatus, FrontierEntry, Priority
from web_crawler.parser import ContentParser, LinkExtractor, normalize_url
from web_crawler.robots import RobotsChecker

__all__ = [
    # Core
    "Crawler",
    "CrawlerConfig",
    # Models
    "CrawlResult",
    "CrawlStatus",
    "FrontierEntry",
    "Priority",
    # Frontier
    "URLFrontier",
    # Fetcher
    "Fetcher",
    "FetchResponse",
    "URLLibFetcher",
    # Robots
    "RobotsChecker",
    # Parser
    "ContentParser",
    "LinkExtractor",
    "normalize_url",
    # Filters
    "URLFilter",
    # Deduplication
    "ContentSeen",
    "URLSeen",
    # Exceptions
    "CrawlerError",
    "ConfigurationError",
    "ContentParseError",
    "FetchError",
    "RobotsDisallowedError",
]
```

### 12. Example (`examples/basic_crawl.py`)

```python
"""Basic crawl example using a mock fetcher for demonstration.

Usage:
    python -m examples.basic_crawl
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
```

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Concurrency model | asyncio + Semaphore | Single-threaded async; no threading complexity; Semaphore caps concurrent downloads |
| URL Frontier front queue | heapq (min-heap) | O(log n) enqueue/dequeue; priority ordering via enum value |
| URL Frontier back queue | dict[host, deque] | O(1) per-host grouping; FIFO within each host |
| Politeness enforcement | Timestamp tracking per host | Simple monotonic clock comparison; no timers needed |
| Fetcher abstraction | ABC with async method | Clean interface for testing (mock fetcher) and custom HTTP clients |
| Default HTTP client | urllib + run_in_executor | Zero dependencies; async-compatible via executor wrapping |
| Robots.txt parsing | stdlib RobotFileParser | Zero dependencies; standard-compliant parsing |
| HTML parsing | stdlib html.parser | Zero dependencies; sufficient for link extraction |
| Content dedup | SHA-256 hex digest set | O(1) lookup; cryptographic hash minimizes false positives |
| URL dedup | Normalized URL string set | O(1) lookup; normalization prevents equivalent-URL duplicates |
| URL normalization | Lowercase scheme/host + strip fragment | Covers most common URL equivalence cases |
| Content hooks | Async callables with kwargs | Flexible; supports both sync and async; exception-isolated |
| Configuration | Single dataclass | Type-safe; validated in __post_init__; sensible defaults |
| Project layout | src/ layout with hatchling | Standard Python packaging; clean import paths |
| Error handling | Custom exception hierarchy | Typed errors enable precise catch blocks; all inherit CrawlerError |
| Link extraction | HTMLParser subclass | Lightweight; handles malformed HTML gracefully |
| Filter ordering | Scheme → length → whitelist → blacklist → extension → custom | Cheapest checks first; whitelist before blacklist per requirements |

## Error Handling

| Error Condition | Exception / Status | When Raised |
|----------------|-------------------|-------------|
| Invalid config values | `ConfigurationError` (ValueError) | CrawlerConfig.__post_init__ with non-positive numerics |
| HTTP fetch failure | `FetchError` | URLLibFetcher on timeout, connection error, non-2xx |
| URL disallowed by robots | `RobotsDisallowedError` | RobotsChecker.is_allowed returns False |
| HTML parse failure | `ContentParseError` | ContentParser encounters unparseable content |
| Fetch error during crawl | CrawlResult with ERROR status | Crawler._process_url catches FetchError |
| Duplicate content | CrawlResult with DUPLICATE status | ContentSeen.is_seen returns True |
| Robots disallowed | CrawlResult with DISALLOWED status | RobotsChecker.is_allowed returns False |
| Hook exception | Logged, crawl continues | Crawler._invoke_hooks catches all exceptions |

All custom exceptions inherit from `CrawlerError` for catch-all handling.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system—essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: URL normalization idempotence

*For any* URL string, normalizing it once and normalizing it again SHALL produce the same result (normalization is idempotent: `normalize(normalize(url)) == normalize(url)`).

**Validates: Requirements 6.4, 8.5**

### Property 2: URL normalization removes fragments and lowercases scheme/host

*For any* URL with a fragment component or mixed-case scheme/host, after normalization the fragment SHALL be empty and the scheme and host SHALL be lowercase, while the path and query remain unchanged.

**Validates: Requirements 6.4, 8.5**

### Property 3: Link extraction completeness

*For any* HTML string containing `<a href="...">` tags, the LinkExtractor SHALL return a list containing exactly one entry for each href attribute value (resolved to absolute and normalized), with no extra or missing entries.

**Validates: Requirements 6.1, 6.3, 6.5**

### Property 4: URL filter scheme rejection

*For any* URL with a scheme other than "http" or "https", the URLFilter SHALL reject it (return False).

**Validates: Requirements 7.7**

### Property 5: URL filter extension blacklist

*For any* URL whose path ends with a blacklisted extension, the URLFilter SHALL reject it regardless of other filter settings.

**Validates: Requirements 7.1**

### Property 6: URL filter domain whitelist/blacklist interaction

*For any* URL, when both a domain whitelist and blacklist are configured, the URL SHALL be accepted only if its domain is in the whitelist AND not in the blacklist.

**Validates: Requirements 7.2, 7.3, 7.6**

### Property 7: URL filter length rejection

*For any* URL whose length exceeds the configured maximum, the URLFilter SHALL reject it.

**Validates: Requirements 7.4**

### Property 8: Content fingerprint determinism

*For any* byte sequence, computing the content fingerprint (SHA-256) multiple times SHALL always produce the same 64-character hex digest string.

**Validates: Requirements 5.3**

### Property 9: Content deduplication correctness

*For any* two byte sequences with identical content, the second SHALL be detected as a duplicate (is_seen returns True after the first is added). For any two byte sequences with different content, they SHALL NOT be considered duplicates.

**Validates: Requirements 5.4, 5.5**

### Property 10: URL deduplication with normalization

*For any* two URLs that differ only in fragment or scheme/host casing, the URLSeen store SHALL treat them as the same URL (second add is a no-op, is_seen returns True for both after either is added).

**Validates: Requirements 8.2, 8.3, 8.5**

### Property 11: Frontier size invariant

*For any* sequence of put and get operations on the URLFrontier, the size property SHALL equal the number of successful puts minus the number of successful gets (non-None returns).

**Validates: Requirements 2.7**

### Property 12: Frontier priority ordering

*For any* set of URLs enqueued with different priorities to the URLFrontier (all from different hosts to avoid politeness delays), dequeuing SHALL return URLs in priority order (HIGH before MEDIUM before LOW).

**Validates: Requirements 2.1, 2.2**

### Property 13: Configuration validation

*For any* non-positive numeric value (zero or negative) supplied for max_pages, max_depth, max_concurrent, per_host_delay, request_timeout, max_url_length, or robots_cache_ttl, the CrawlerConfig SHALL raise a ValueError.

**Validates: Requirements 12.4**

### Property 14: Crawl result status correctness

*For any* URL processed by the crawler: if robots.txt disallows it, the result status SHALL be "disallowed"; if the fetch fails, the status SHALL be "error" with an error message; if the content fingerprint was previously seen, the status SHALL be "duplicate"; if the fetch succeeds with new content, the status SHALL be "success".

**Validates: Requirements 11.2, 11.3, 11.4, 11.5, 3.4, 3.7**
