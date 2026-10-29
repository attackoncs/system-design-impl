# Implementation Plan: Web Crawler

## Overview

Implement a Python asyncio-based web crawler library following the pipeline architecture: URL Frontier → Robots Check → Fetch → Parse → Deduplicate → Extract Links → Filter → Enqueue. The project uses src layout with zero runtime dependencies (stdlib only), pytest + pytest-asyncio + hypothesis for testing, and hatchling for packaging.

## Tasks

- [ ] 1. Set up project structure and foundational modules
  - [ ] 1.1 Create project skeleton with pyproject.toml and directory structure
    - Create `web-crawler/pyproject.toml` with hatchling build system, zero runtime deps, dev deps (pytest, pytest-asyncio, hypothesis)
    - Create `src/web_crawler/__init__.py` (empty initially), `tests/__init__.py`, `docs/`, `examples/` directories
    - _Requirements: NFR-1.1, NFR-1.2, NFR-1.3, NFR-1.4, NFR-1.5, NFR-1.6, NFR-1.7_

  - [ ] 1.2 Implement custom exceptions module (`src/web_crawler/exceptions.py`)
    - Define `CrawlerError`, `ConfigurationError`, `FetchError`, `RobotsDisallowedError`, `ContentParseError`
    - All exceptions inherit from `CrawlerError`; `FetchError` stores url and reason
    - _Requirements: NFR-2.5_

  - [ ] 1.3 Implement data models (`src/web_crawler/models.py`)
    - Define `CrawlStatus` enum (SUCCESS, ERROR, DUPLICATE, DISALLOWED, FILTERED)
    - Define `Priority` enum (HIGH=0, MEDIUM=1, LOW=2)
    - Define `FrontierEntry` dataclass with url, priority, depth, host; implement `__lt__` for heapq
    - Define `CrawlResult` dataclass with all fields per design
    - _Requirements: 11.1, 2.1, 2.8_

  - [ ] 1.4 Implement configuration (`src/web_crawler/config.py`)
    - Define `CrawlerConfig` dataclass with all settings and sensible defaults
    - Implement `__post_init__` validation raising ValueError for non-positive numerics
    - _Requirements: 12.1, 12.2, 12.3, 12.4_

- [ ] 2. Implement URL Frontier
  - [ ] 2.1 Implement URL Frontier (`src/web_crawler/frontier.py`)
    - Implement `URLFrontier` with heapq-based front queue and dict[host, deque] back queues
    - Implement `put()`, `get()`, `is_empty()`, `size` property
    - Enforce per-host politeness delay using monotonic timestamps
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8_

  - [ ] 2.2 Write property test for Frontier size invariant
    - **Property 11: Frontier size invariant**
    - **Validates: Requirements 2.7**

  - [ ] 2.3 Write property test for Frontier priority ordering
    - **Property 12: Frontier priority ordering**
    - **Validates: Requirements 2.1, 2.2**

- [ ] 3. Implement Fetcher interface and HTTP client
  - [ ] 3.1 Implement Fetcher ABC and URLLibFetcher (`src/web_crawler/fetcher.py`)
    - Define `FetchResponse` dataclass (status_code, headers, body, url)
    - Define `Fetcher` ABC with async `fetch()` method
    - Implement `URLLibFetcher` using urllib.request wrapped in asyncio executor
    - Handle HTTPError, URLError, TimeoutError → raise FetchError
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 3.1, 3.2, 3.5, 3.6, NFR-4.4_

- [ ] 4. Implement Robots.txt checker
  - [ ] 4.1 Implement RobotsChecker (`src/web_crawler/robots.py`)
    - Implement per-host caching with configurable TTL
    - Use stdlib `RobotFileParser` for parsing
    - Allow all paths if robots.txt cannot be fetched
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

- [ ] 5. Implement content parsing and link extraction
  - [ ] 5.1 Implement ContentParser and LinkExtractor (`src/web_crawler/parser.py`)
    - Implement `normalize_url()` function (lowercase scheme/host, remove fragments)
    - Implement `LinkExtractorParser` (HTMLParser subclass extracting href from `<a>` tags)
    - Implement `ContentParser.is_html()` checking Content-Type or content sniffing
    - Implement `LinkExtractor.extract()` resolving relative URLs and normalizing
    - _Requirements: 5.1, 5.2, 6.1, 6.2, 6.3, 6.4, 6.5_

  - [ ] 5.2 Write property test for URL normalization idempotence
    - **Property 1: URL normalization idempotence**
    - **Validates: Requirements 6.4, 8.5**

  - [ ] 5.3 Write property test for URL normalization removes fragments and lowercases
    - **Property 2: URL normalization removes fragments and lowercases scheme/host**
    - **Validates: Requirements 6.4, 8.5**

  - [ ] 5.4 Write property test for link extraction completeness
    - **Property 3: Link extraction completeness**
    - **Validates: Requirements 6.1, 6.3, 6.5**

- [ ] 6. Implement URL filtering
  - [ ] 6.1 Implement URLFilter (`src/web_crawler/filters.py`)
    - Implement filter chain: scheme → length → whitelist → blacklist → extension → custom
    - Support configurable extension blacklist, domain whitelist/blacklist, max URL length, custom filters
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7_

  - [ ] 6.2 Write property test for URL filter scheme rejection
    - **Property 4: URL filter scheme rejection**
    - **Validates: Requirements 7.7**

  - [ ] 6.3 Write property test for URL filter extension blacklist
    - **Property 5: URL filter extension blacklist**
    - **Validates: Requirements 7.1**

  - [ ] 6.4 Write property test for URL filter domain whitelist/blacklist interaction
    - **Property 6: URL filter domain whitelist/blacklist interaction**
    - **Validates: Requirements 7.2, 7.3, 7.6**

  - [ ] 6.5 Write property test for URL filter length rejection
    - **Property 7: URL filter length rejection**
    - **Validates: Requirements 7.4**

- [ ] 7. Implement deduplication
  - [ ] 7.1 Implement ContentSeen and URLSeen (`src/web_crawler/dedup.py`)
    - Implement `ContentSeen` with SHA-256 fingerprint set
    - Implement `URLSeen` with normalized URL set
    - _Requirements: 5.3, 5.4, 5.5, 5.6, 8.1, 8.2, 8.3, 8.4, 8.5_

  - [ ] 7.2 Write property test for content fingerprint determinism
    - **Property 8: Content fingerprint determinism**
    - **Validates: Requirements 5.3**

  - [ ] 7.3 Write property test for content deduplication correctness
    - **Property 9: Content deduplication correctness**
    - **Validates: Requirements 5.4, 5.5**

  - [ ] 7.4 Write property test for URL deduplication with normalization
    - **Property 10: URL deduplication with normalization**
    - **Validates: Requirements 8.2, 8.3, 8.5**

- [ ] 8. Checkpoint - Verify component tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 9. Implement Crawler orchestrator
  - [ ] 9.1 Implement Crawler class (`src/web_crawler/crawler.py`)
    - Implement async `crawl()` method with seed URL enqueuing and main crawl loop
    - Implement `_process_url()` pipeline: robots → fetch → dedup → parse → extract → filter → enqueue
    - Implement `_enqueue_url()` with URL seen check
    - Implement `_invoke_hooks()` supporting sync and async hooks with exception isolation
    - Use asyncio.Semaphore for concurrency limiting
    - Enforce max_pages and max_depth stop conditions
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 3.3, 3.4, 3.7, 3.8, 10.1, 10.2, 10.3, 10.4, 10.5, NFR-4.1, NFR-4.2, NFR-4.3_

  - [ ] 9.2 Write property test for configuration validation
    - **Property 13: Configuration validation**
    - **Validates: Requirements 12.4**

  - [ ] 9.3 Write property test for crawl result status correctness
    - **Property 14: Crawl result status correctness**
    - **Validates: Requirements 11.2, 11.3, 11.4, 11.5, 3.4, 3.7**

- [ ] 10. Wire up public API and unit tests
  - [ ] 10.1 Implement public API exports (`src/web_crawler/__init__.py`)
    - Export all public classes, functions, and exceptions per design
    - _Requirements: NFR-1.8_

  - [ ] 10.2 Write unit tests for models and config (`tests/test_models.py`, `tests/test_config.py`)
    - Test CrawlStatus, Priority, FrontierEntry ordering, CrawlResult fields
    - Test CrawlerConfig defaults and validation
    - _Requirements: 11.1, 12.1, 12.2, 12.3, 12.4_

  - [ ] 10.3 Write unit tests for frontier (`tests/test_frontier.py`)
    - Test put/get, priority ordering, politeness delay, empty/size
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_

  - [ ] 10.4 Write unit tests for fetcher (`tests/test_fetcher.py`)
    - Test FetchResponse creation, URLLibFetcher error handling (mock-based)
    - _Requirements: 9.1, 9.2, 3.7_

  - [ ] 10.5 Write unit tests for robots checker (`tests/test_robots.py`)
    - Test allowed/disallowed paths, caching, fetch failure fallback
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

  - [ ] 10.6 Write unit tests for parser and link extractor (`tests/test_parser.py`)
    - Test normalize_url, is_html, link extraction with relative/absolute URLs
    - _Requirements: 5.1, 5.2, 6.1, 6.2, 6.3, 6.4, 6.5_

  - [ ] 10.7 Write unit tests for URL filter (`tests/test_filters.py`)
    - Test scheme rejection, extension blacklist, domain whitelist/blacklist, length, custom filters
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7_

  - [ ] 10.8 Write unit tests for deduplication (`tests/test_dedup.py`)
    - Test ContentSeen fingerprint and is_seen, URLSeen normalization
    - _Requirements: 5.3, 5.4, 5.5, 5.6, 8.1, 8.2, 8.3, 8.4, 8.5_

  - [ ] 10.9 Write unit tests for crawler orchestrator (`tests/test_crawler.py`)
    - Test full crawl loop with mock fetcher, max_pages, max_depth, hooks, error handling
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 10.1, 10.2, 10.3, 10.4, 10.5_

- [ ] 11. Checkpoint - Full test suite passes
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 12. Add example and documentation
  - [ ] 12.1 Create example script (`examples/basic_crawl.py`)
    - Implement MockFetcher with predefined pages
    - Demonstrate Crawler usage with config, seed URLs, and result printing
    - _Requirements: NFR-1.7_

  - [ ] 12.2 Copy spec documents to docs/ directory
    - Copy requirements.md and design.md to `docs/`; create `docs/tasks.md` summary
    - _Requirements: NFR-1.6_

- [ ] 13. Final checkpoint - All tests pass and package installs cleanly
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document (14 properties total)
- Unit tests validate specific examples and edge cases
- The project uses Python with asyncio; zero runtime dependencies (stdlib only)
- All property tests go in `tests/test_properties.py`; unit tests in individual test files

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4"] },
    { "id": 2, "tasks": ["2.1", "3.1", "5.1", "6.1", "7.1"] },
    { "id": 3, "tasks": ["2.2", "2.3", "4.1", "5.2", "5.3", "5.4", "6.2", "6.3", "6.4", "6.5", "7.2", "7.3", "7.4"] },
    { "id": 4, "tasks": ["9.1"] },
    { "id": 5, "tasks": ["9.2", "9.3", "10.1"] },
    { "id": 6, "tasks": ["10.2", "10.3", "10.4", "10.5", "10.6", "10.7", "10.8", "10.9"] },
    { "id": 7, "tasks": ["12.1", "12.2"] }
  ]
}
```
