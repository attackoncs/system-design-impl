# Web Crawler

An asyncio-based Python web crawler library implementing the design from System Design Interview Chapter 10 "Design A Web Crawler". Features a pipeline architecture with URL frontier (priority + politeness), robots.txt compliance, content deduplication, link extraction, and pluggable components — all with zero runtime dependencies (stdlib only).

## Features

- **Async crawl loop** with configurable concurrency via asyncio.Semaphore
- **URL Frontier** with priority front queues (HIGH/MEDIUM/LOW) and per-host politeness back queues
- **Robots.txt compliance** with per-host caching and configurable TTL
- **Content deduplication** using SHA-256 fingerprinting
- **Link extraction** with relative URL resolution and normalization
- **Configurable URL filtering** — extension blacklist, domain whitelist/blacklist, max length, custom filters
- **URL deduplication** via normalized URL set (O(1) lookup)
- **Pluggable Fetcher interface** — swap in custom HTTP clients or mock fetchers for testing
- **Content processing hooks** — sync and async callbacks for each crawled page
- **Zero runtime dependencies** — uses only Python standard library
- **Structured results** — typed `CrawlResult` with status, links, fingerprint, and metadata

## Architecture

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

### Pipeline Flow

```
Seed URLs → Frontier → Robots Check → Fetch → Content Dedup → Parse HTML
    → Extract Links → URL Filter → URL Dedup → Enqueue → (repeat)
```

## Installation

```bash
cd web-crawler
pip install -e .
```

With development dependencies:

```bash
pip install -e ".[dev]"
```

### Requirements

- Python >= 3.9

## Quick Start

```python
import asyncio
from web_crawler import Crawler, CrawlerConfig, Fetcher, FetchResponse


class MockFetcher(Fetcher):
    """Mock fetcher with predefined pages for demonstration."""

    def __init__(self):
        self._pages = {
            "http://example.com": '<html><body><a href="/about">About</a></body></html>',
            "http://example.com/about": '<html><body><h1>About</h1></body></html>',
            "http://example.com/robots.txt": "User-agent: *\nAllow: /\n",
        }

    async def fetch(self, url, headers=None, timeout=30.0):
        if url in self._pages:
            body = self._pages[url].encode()
            ct = "text/plain" if url.endswith("robots.txt") else "text/html"
            return FetchResponse(status_code=200, headers={"Content-Type": ct}, body=body, url=url)
        from web_crawler.exceptions import FetchError
        raise FetchError(url, "Not found")


async def main():
    config = CrawlerConfig(
        max_pages=10,
        max_depth=2,
        max_concurrent=3,
        per_host_delay=0.1,
        domain_whitelist=["example.com"],
    )
    crawler = Crawler(config=config, fetcher=MockFetcher())
    results = await crawler.crawl(["http://example.com"])

    for r in results:
        print(f"[{r.status.value}] {r.url} (depth={r.depth}, links={len(r.links)})")


asyncio.run(main())
```

## API Reference

### Crawler

```python
Crawler(config: CrawlerConfig = None, fetcher: Fetcher = None)
```

| Method | Description |
|--------|-------------|
| `await crawl(seed_urls: list[str]) -> list[CrawlResult]` | Run the crawl loop from seed URLs |

### CrawlerConfig

```python
CrawlerConfig(
    max_pages: int = 100,           # Maximum pages to crawl
    max_depth: int = 3,             # Maximum link depth from seeds
    max_concurrent: int = 10,       # Concurrent download limit
    per_host_delay: float = 1.0,    # Seconds between requests to same host
    request_timeout: float = 30.0,  # HTTP request timeout
    user_agent: str = "WebCrawler/1.0",
    robots_cache_ttl: float = 3600.0,
    extension_blacklist: list[str] = [...],  # File extensions to skip
    domain_whitelist: list[str] | None = None,
    domain_blacklist: list[str] | None = None,
    max_url_length: int = 2048,
    custom_filters: list[Callable] = [],
    content_hooks: list[Callable] = [],
)
```

### CrawlResult

| Field | Type | Description |
|-------|------|-------------|
| `url` | `str` | The crawled URL |
| `status` | `CrawlStatus` | SUCCESS, ERROR, DUPLICATE, DISALLOWED, FILTERED |
| `http_status` | `int \| None` | HTTP response status code |
| `content_length` | `int \| None` | Response body size in bytes |
| `content_fingerprint` | `str \| None` | SHA-256 hex digest |
| `links` | `list[str]` | Extracted links from the page |
| `depth` | `int` | Crawl depth from seed URL |
| `error_message` | `str \| None` | Error details (when status is ERROR) |
| `headers` | `dict[str, str]` | Response headers |

### Fetcher (ABC)

```python
class Fetcher(ABC):
    @abstractmethod
    async def fetch(self, url: str, headers: dict | None = None, timeout: float = 30.0) -> FetchResponse: ...
```

Implement this interface to provide custom HTTP clients. The default `URLLibFetcher` uses `urllib.request` wrapped in `asyncio.run_in_executor`.

### URLFrontier

```python
URLFrontier(per_host_delay: float = 1.0)
```

| Method | Description |
|--------|-------------|
| `put(entry: FrontierEntry)` | Add a URL to the frontier |
| `get() -> FrontierEntry \| None` | Dequeue next URL (respects politeness) |
| `is_empty() -> bool` | Check if frontier has pending URLs |
| `size -> int` | Number of pending URLs |

### Exceptions

| Exception | Description |
|-----------|-------------|
| `CrawlerError` | Base exception for all crawler errors |
| `ConfigurationError` | Invalid configuration values |
| `FetchError` | HTTP fetch failure (stores url and reason) |
| `RobotsDisallowedError` | URL disallowed by robots.txt |
| `ContentParseError` | HTML content cannot be parsed |

## Running Tests

```bash
pip install -e ".[dev]"
pytest                              # All tests
pytest tests/test_crawler.py        # Crawler orchestrator tests
pytest tests/test_frontier.py       # URL frontier tests
pytest tests/test_properties.py     # Property-based tests (Hypothesis)
pytest --hypothesis-show-statistics # Detailed PBT stats
```

### Test Coverage

- **Unit tests**: All components tested individually (config, models, frontier, fetcher, robots, parser, filters, dedup, crawler)
- **Property-based tests**: 14 Hypothesis properties covering URL normalization, filtering, deduplication, frontier invariants, and crawl result correctness

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
│       ├── __init__.py       # Public API exports
│       ├── config.py         # CrawlerConfig dataclass
│       ├── models.py         # CrawlResult, CrawlStatus, Priority, FrontierEntry
│       ├── exceptions.py     # Custom exception hierarchy
│       ├── frontier.py       # URLFrontier (priority + politeness)
│       ├── fetcher.py        # Fetcher ABC + URLLibFetcher
│       ├── robots.py         # RobotsChecker with caching
│       ├── parser.py         # ContentParser + LinkExtractor
│       ├── filters.py        # URLFilter with configurable rules
│       ├── dedup.py          # ContentSeen (SHA-256) + URLSeen
│       └── crawler.py        # Main Crawler orchestrator
├── tests/
│   ├── test_config.py
│   ├── test_models.py
│   ├── test_frontier.py
│   ├── test_fetcher.py
│   ├── test_robots.py
│   ├── test_parser.py
│   ├── test_filters.py
│   ├── test_dedup.py
│   ├── test_crawler.py
│   └── test_properties.py   # Hypothesis property-based tests
└── examples/
    └── basic_crawl.py        # Demo with mock fetcher
```

## References

- Alex Xu, "System Design Interview" — Chapter 10: Design A Web Crawler

## License

MIT
