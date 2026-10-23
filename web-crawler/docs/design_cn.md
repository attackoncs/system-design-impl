# 设计文档：网页爬虫（Web Crawler）

## 架构概览（Architecture Overview）

网页爬虫库采用管道架构，每个阶段都有可插拔的组件。主编排器（`Crawler`）负责协调爬取循环：出队 URL → 检查 robots → 抓取 → 解析 → 去重（Deduplication） → 提取链接 → 过滤 → 入队。每个阶段都由抽象接口支撑，便于在测试和扩展时使用自定义实现。

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

## 项目结构

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
│       ├── __init__.py          # 公共 API 导出
│       ├── config.py            # CrawlerConfig dataclass
│       ├── models.py            # 数据模型（CrawlResult, CrawlStatus, Priority, FrontierEntry）
│       ├── exceptions.py        # 自定义异常层次结构
│       ├── frontier.py          # URLFrontier，包含 front 队列和 back 队列
│       ├── fetcher.py           # Fetcher ABC + 默认 urllib 实现
│       ├── robots.py            # RobotsChecker，支持 per-host 缓存
│       ├── parser.py            # ContentParser + LinkExtractor
│       ├── filters.py           # URLFilter，支持可配置规则
│       ├── dedup.py             # ContentSeen（SHA-256）+ URLSeen（基于集合）
│       └── crawler.py           # 主编排器 Crawler（异步爬取循环）
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
│   └── test_properties.py      # 基于属性的测试（Hypothesis）
└── examples/
    └── basic_crawl.py           # 使用 mock fetcher 的简单爬取示例
```

## 组件设计

### 1. 自定义异常（`exceptions.py`）

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

### 2. 数据模型（`models.py`）

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

### 3. 配置（`config.py`）

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

### 4. URL 前沿（URL Frontier，`frontier.py`）

URL Frontier 是爬虫的 URL 调度中心，它由 front 队列和 back 队列两部分组成。front 队列使用优先堆（heapq）来决定爬取顺序，back 队列则按主机分组，用于实现对同一主机的礼貌延迟（Politeness Policy）。

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

### 5. Fetcher 接口（`fetcher.py`）

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

### 6. Robots 检查器（Robots Checker，`robots.py`）

该组件负责检查 `robots.txt` 合规性，对每个主机首次访问时抓取并解析 robots.txt，然后按配置的 TTL 缓存结果。

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

### 7. 内容解析器与链接提取器（Content Parser and Link Extractor，`parser.py`）

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

### 8. URL 过滤器（URL Filter，`filters.py`）

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

### 9. 去重（Deduplication，`dedup.py`）

系统提供两种去重机制：**URL 去重**（URL Dedup）防止重复访问同一 URL，**内容去重**（Content Dedup）检测页面内容是否已被抓取。内容去重基于 SHA-256 指纹集合实现 O(1) 查找。

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

### 10. 爬虫编排器（Crawler Orchestrator，`crawler.py`）

这是整个爬虫的主控制器，负责协调完整的爬取流水线：从 Frontier 获取 URL → 检查 robots.txt → 抓取 → 解析 → 内容去重 → 提取链接 → 过滤 → 入队。使用 `asyncio.Semaphore` 控制并发下载数量。

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

### 11. 公共 API（`__init__.py`）

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

### 12. 示例（`examples/basic_crawl.py`）

该示例使用 mock fetcher 演示基本的爬取流程，方便离线测试。

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

## 设计决策（Design Decisions）

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 并发模型（Concurrency model） | asyncio + Semaphore | 单线程异步，避免多线程复杂性；Semaphore 限制并发下载数量 |
| URL Frontier front 队列 | heapq（最小堆） | O(log n) 的入队/出队操作；通过枚举值实现优先级排序 |
| URL Frontier back 队列 | dict[host, deque] | O(1) 的 per-host 分组；每个主机内部按 FIFO 顺序 |
| 礼貌策略执行（Politeness enforcement） | 每主机时间戳跟踪 | 简单的单调时钟比较，无需定时器 |
| Fetcher 抽象 | ABC + async 方法 | 干净的抽象接口，便于测试（mock fetcher）和自定义 HTTP 客户端 |
| 默认 HTTP 客户端 | urllib + run_in_executor | 零外部依赖；通过 executor 包装实现异步兼容 |
| Robots.txt 解析 | 标准库 RobotFileParser | 零外部依赖；符合标准规范的解析 |
| HTML 解析 | 标准库 html.parser | 零外部依赖；足以满足链接提取需求 |
| 内容去重（Content dedup） | SHA-256 十六进制摘要集合 | O(1) 查找；加密哈希最小化误判可能 |
| URL 去重（URL dedup） | 规范化 URL 字符串集合 | O(1) 查找；规范化处理避免等价 URL 的重复访问 |
| URL 规范化（URL normalization） | scheme/host 小写化 + 移除 fragment | 覆盖最常见的 URL 等价情形 |
| 内容钩子（Content hooks） | 支持 sync/async callable，传入 kwargs | 灵活设计；同时支持同步和异步；异常相互隔离 |
| 配置方式 | 单一 dataclass | 类型安全；在 `__post_init__` 中校验；提供合理默认值 |
| 项目布局 | src/ 布局 + hatchling | 标准 Python 打包方式；清晰的导入路径 |
| 错误处理 | 自定义异常层次结构 | 类型化错误支持精确捕获；所有异常继承自 CrawlerError |
| 链接提取 | HTMLParser 子类 | 轻量实现；能够优雅处理不规范 HTML |
| 过滤器执行顺序 | scheme → length → whitelist → blacklist → extension → custom | 先执行成本最低的检查；按需求要求 whitelist 优先于 blacklist |

## 错误处理（Error Handling）

| 错误场景 | 异常 / 状态 | 触发时机 |
|----------|-------------|----------|
| 配置值非法 | `ConfigurationError`（ValueError） | CrawlerConfig.__post_init__ 中传入非正数时 |
| HTTP 抓取失败 | `FetchError` | URLLibFetcher 发生超时、连接错误或非 2xx 响应时 |
| URL 被 robots 禁止 | `RobotsDisallowedError` | RobotsChecker.is_allowed 返回 False |
| HTML 解析失败 | `ContentParseError` | ContentParser 遇到无法解析的内容 |
| 爬取过程中的抓取错误 | CrawlResult，状态为 ERROR | Crawler._process_url 捕获 FetchError |
| 重复内容 | CrawlResult，状态为 DUPLICATE | ContentSeen.is_seen 返回 True |
| Robots 禁止访问 | CrawlResult，状态为 DISALLOWED | RobotsChecker.is_allowed 返回 False |
| Hook 异常 | 记录日志，爬取继续 | Crawler._invoke_hooks 捕获所有异常 |

所有自定义异常均继承自 `CrawlerError`，便于统一捕获处理。

## 正确性属性（Correctness Properties）

> **属性（Property）** 是系统在所有合法执行过程中都应保持的特征或行为——本质上是对系统应当做什么的形式化陈述。属性是连接人类可读规格与机器可验证正确性保证的桥梁。

### 属性 1：URL 规范化幂等性（URL normalization idempotence）

**对于任意** URL 字符串，规范化一次后再规范化一次 **应当** 产生相同的结果（规范化是幂等的：`normalize(normalize(url)) == normalize(url)`）。

**对应需求：6.4、8.5**

### 属性 2：URL 规范化移除 fragment 并小写 scheme/host

**对于任意** 包含 fragment 组件或混合大小写 scheme/host 的 URL，规范化后 fragment **应当** 为空，scheme 和 host **应当** 为小写，而 path 和 query 保持不变。

**对应需求：6.4、8.5**

### 属性 3：链接提取完备性（Link extraction completeness）

**对于任意** 包含 `<a href="...">` 标签的 HTML 字符串，LinkExtractor **应当** 返回一个列表，其中每个 href 属性值（已解析为绝对路径并规范化）恰好对应一个条目，不多也不少。

**对应需求：6.1、6.3、6.5**

### 属性 4：URL 过滤器 scheme 拒绝

**对于任意** scheme 不是 "http" 或 "https" 的 URL，URLFilter **应当** 拒绝它（返回 False）。

**对应需求：7.7**

### 属性 5：URL 过滤器扩展名黑名单

**对于任意** 路径以黑名单中扩展名结尾的 URL，URLFilter **应当** 忽略其他过滤器设置而拒绝它。

**对应需求：7.1**

### 属性 6：URL 过滤器域名白名单/黑名单交互

**对于任意** URL，当同时配置域名白名单和黑名单时，URL **应当** 仅在其域名同时在白名单中 **且** 不在黑名单中时才被接受。

**对应需求：7.2、7.3、7.6**

### 属性 7：URL 过滤器长度拒绝

**对于任意** 长度超过配置最大值的 URL，URLFilter **应当** 拒绝它。

**对应需求：7.4**

### 属性 8：内容指纹确定性（Content fingerprint determinism）

**对于任意** 字节序列，多次计算其内容指纹（SHA-256）**应当** 始终产生相同的 64 字符十六进制摘要字符串。

**对应需求：5.3**

### 属性 9：内容去重正确性（Content deduplication correctness）

**对于任意** 两个内容完全相同的字节序列，第二个 **应当** 被检测为重复（第一个被添加后 is_seen 返回 True）。对于任意两个内容不同的字节序列，它们 **不应** 被视为重复。

**对应需求：5.4、5.5**

### 属性 10：带规范化的 URL 去重

**对于任意** 两个仅在 fragment 或 scheme/host 大小写方面不同的 URL，URLSeen 存储 **应当** 将它们视为同一 URL（第二个 add 是无操作，任一被添加后 is_seen 对两者都返回 True）。

**对应需求：8.2、8.3、8.5**

### 属性 11：Frontier 大小不变式（Frontier size invariant）

**对于任意** 一系列 put 和 get 操作，URLFrontier 的 size 属性 **应当** 等于成功的 put 次数减去成功的 get 次数（非 None 返回）。

**对应需求：2.7**

### 属性 12：Frontier 优先级排序

**对于任意** 一组入队时具有不同优先级的 URL（均来自不同主机，以避免礼貌延迟干扰），出队时 **应当** 按优先级顺序返回（HIGH 在 MEDIUM 之前，MEDIUM 在 LOW 之前）。

**对应需求：2.1、2.2**

### 属性 13：配置校验（Configuration validation）

**对于任意** 传入 max_pages、max_depth、max_concurrent、per_host_delay、request_timeout、max_url_length 或 robots_cache_ttl 的非正数值（零或负数），CrawlerConfig **应当** 抛出 ValueError。

**对应需求：12.4**

### 属性 14：爬取结果状态正确性（Crawl result status correctness）

**对于任意** 由爬虫处理的 URL：如果 robots.txt 禁止访问，结果状态 **应当** 为 "disallowed"；如果抓取失败，状态 **应当** 为 "error" 并附带错误信息；如果内容指纹已存在，状态 **应当** 为 "duplicate"；如果抓取成功且内容是新的，状态 **应当** 为 "success"。

**对应需求：11.2、11.3、11.4、11.5、3.4、3.7**
