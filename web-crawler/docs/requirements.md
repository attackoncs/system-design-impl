# Requirements Document

## Introduction

A Python web crawler library implementing the design from System Design Interview Chapter 10 "Design A Web Crawler". The library provides a single-process, asyncio-based web crawler with URL frontier (priority front-queues and politeness back-queues), HTML downloading with robots.txt compliance, content deduplication via SHA-256 hashing, link extraction, URL filtering, and URL deduplication. The core is a pure library with zero runtime dependencies (stdlib only); configurable interfaces allow custom fetchers, storage backends, and content processing hooks.

## Glossary

- **Crawler**: The top-level orchestrator that coordinates the crawl loop: dequeue URL → download → parse → extract links → enqueue new URLs
- **URL_Frontier**: A priority-aware, politeness-enforcing queue that manages URLs to be crawled, combining front queues (priority) and back queues (per-host politeness)
- **Front_Queue**: A set of priority-based queues within the URL Frontier that determine the order in which URLs are selected for crawling (HIGH, MEDIUM, LOW)
- **Back_Queue**: A set of per-host queues within the URL Frontier that enforce politeness delays between requests to the same host
- **HTML_Downloader**: The component responsible for fetching web page content via HTTP, respecting robots.txt rules and rate limits
- **Robots_Checker**: A component that parses and caches robots.txt files to determine whether a URL is allowed to be crawled
- **Content_Parser**: A component that validates downloaded HTML content and extracts text/structure for processing
- **Content_Fingerprint**: A SHA-256 hash of page content used to detect duplicate pages (content seen check)
- **Link_Extractor**: A component that parses HTML to extract hyperlinks (href attributes) from anchor tags
- **URL_Filter**: A configurable component that determines whether an extracted URL should be added to the frontier based on rules (extension blacklist, domain whitelist/blacklist, max depth)
- **URL_Seen_Store**: A data structure (in-memory set) that tracks which URLs have already been visited or enqueued to avoid redundant crawling
- **Crawl_Result**: A data structure representing the outcome of crawling a single URL (status, content, extracted links, metadata)
- **Fetcher**: An abstract interface for HTTP fetching, allowing custom implementations for testing or alternative HTTP clients
- **Content_Hook**: A callback interface for processing crawled content (plugin/hook system)

## Requirements

### Requirement 1: Crawl Orchestration

**User Story:** As a developer, I want a high-level crawler that coordinates the entire crawl loop, so that I can crawl websites by providing seed URLs and configuration.

#### Acceptance Criteria

1.1 WHEN seed URLs are provided, THE Crawler SHALL add them to the URL_Frontier and begin the crawl loop.

1.2 THE Crawler SHALL repeatedly dequeue URLs from the URL_Frontier, download their content, parse it, extract links, filter them, and enqueue new URLs until the frontier is empty or a stop condition is met.

1.3 max_pages bounds attempts per batch, including errors, disallowed and duplicate URLs; pending work remains for later batches.

1.4 THE Crawler SHALL support a configurable maximum crawl depth from seed URLs, after which deeper URLs are not enqueued.

1.5 THE Crawler SHALL support a configurable maximum number of concurrent downloads using asyncio.

1.6 WHEN the URL_Frontier is empty and all in-flight downloads are complete, THE Crawler SHALL terminate gracefully.

1.7 crawl() returns all attempted results from this batch, not just successes; one instance rejects concurrent crawl calls.

1.8 THE Crawler SHALL invoke registered Content_Hooks for each successfully downloaded and parsed page.

### Requirement 2: URL Frontier

**User Story:** As a developer, I want a URL frontier with priority and politeness support, so that important URLs are crawled first and hosts are not overwhelmed with requests.

#### Acceptance Criteria

2.1 THE URL_Frontier SHALL maintain front queues that prioritize URLs into HIGH, MEDIUM, and LOW priority levels.

2.2 Choose HIGH/MEDIUM/LOW among eligible host heads with FIFO ties; reserved/delayed hosts do not block ready hosts.

2.3 THE URL_Frontier SHALL maintain back queues that group URLs by their host (domain).

2.4 Reserve at most one task per host; start the next politeness delay on completion, default one second; first requests have no previous access to wait for.

2.5 Use per-host priority heaps, a ready-head heap and a delayed-host wake-up heap; do not scan a reserved-host backlog on each poll.

2.6 Return only unreserved hosts whose delay has elapsed; completion republishes the next host head.

2.7 THE URL_Frontier SHALL provide methods to check if it is empty and to get the current size (number of pending URLs).

2.8 THE URL_Frontier SHALL support adding URLs with an associated priority level and crawl depth.

### Requirement 3: HTML Downloading

**User Story:** As a developer, I want an HTML downloader that fetches pages via HTTP and respects robots.txt, so that crawling is both functional and ethical.

#### Acceptance Criteria

3.1 THE HTML_Downloader SHALL fetch web pages using HTTP GET requests via the configured Fetcher interface.

3.2 THE HTML_Downloader SHALL use `urllib.request` from the Python standard library as the default Fetcher implementation.

3.3 BEFORE downloading a URL, THE HTML_Downloader SHALL check the Robots_Checker to verify the URL is allowed.

3.4 IF a URL is disallowed by robots.txt, THEN THE HTML_Downloader SHALL skip that URL and return a Crawl_Result with a "disallowed" status.

3.5 THE HTML_Downloader SHALL support a configurable request timeout (default: 30 seconds).

3.6 THE HTML_Downloader SHALL set a configurable User-Agent header on all requests (default: "WebCrawler/1.0").

3.7 IF an HTTP error occurs (timeout, connection error, non-2xx status), THEN THE HTML_Downloader SHALL return a Crawl_Result with an "error" status and the error details.

3.8 THE HTML_Downloader SHALL accept any implementation conforming to the Fetcher interface, enabling custom HTTP clients or mock fetchers for testing.

### Requirement 4: Robots.txt Compliance

**User Story:** As a developer, I want the crawler to respect robots.txt rules, so that it behaves ethically and avoids crawling disallowed paths.

#### Acceptance Criteria

4.1 THE Robots_Checker SHALL fetch and parse robots.txt for each host before the first request to that host.

4.2 Reuse RobotFileParser for groups, extending wildcard, terminal-anchor and longest-rule matching.

4.3 Cache per origin (scheme/host/port); striped locks coalesce concurrent cache misses.

4.4 WHEN checking if a URL is allowed, THE Robots_Checker SHALL use the configured User-Agent string for matching rules.

4.5 Retain the network-error allow policy: cache unavailable results and allow on FetchError/OSError; full RFC HTTP failure-status policies are not claimed.

4.6 THE Robots_Checker SHALL support a configurable cache TTL (time-to-live) for robots.txt entries (default: 3600 seconds).

4.7 Support *, terminal $, longest-rule and equal-length Allow precedence. Keep literal %2A/%24 distinct from operators; parse URLs before decoding escaped delimiters. Retain stdlib group selection without all RFC group-merging behavior.

### Requirement 5: Content Parsing and Deduplication

**User Story:** As a developer, I want content parsing and deduplication, so that the crawler processes valid HTML and avoids storing duplicate page content.

#### Acceptance Criteria

5.1 THE Content_Parser SHALL validate that downloaded content is HTML (check Content-Type header or content sniffing).

5.2 THE Content_Parser SHALL use `html.parser.HTMLParser` from the Python standard library to parse HTML content.

5.3 AFTER downloading a page, THE Crawler SHALL compute a Content_Fingerprint (SHA-256 hash) of the page body.

5.4 Mark duplicates DUPLICATE without repeated archiving/hooks, but still discover links against the current document URL.

5.5 Claim new fingerprints before processing and publish only after acceptance; concurrent duplicates wait and can retry after owner failure/cancellation.

5.6 THE content seen store SHALL use an in-memory set of SHA-256 hex digests as the default implementation.

### Requirement 6: Link Extraction

**User Story:** As a developer, I want to extract links from crawled pages, so that the crawler can discover and follow new URLs.

#### Acceptance Criteria

6.1 THE Link_Extractor SHALL parse HTML content and extract all `href` attribute values from `<a>` tags.

6.2 THE Link_Extractor SHALL use `html.parser.HTMLParser` from the Python standard library.

6.3 Resolve relative URLs against the first base href and document address; missing/malformed base falls back to the document URL, and later base elements do not override the first.

6.4 Remove fragments and lowercase scheme/host while preserving path, query and case-sensitive userinfo.

6.5 THE Link_Extractor SHALL return a list of absolute, normalized URL strings.

6.6 Skip individually malformed hrefs without discarding valid siblings; seeds and discovered links share the filter.

### Requirement 7: URL Filtering

**User Story:** As a developer, I want configurable URL filters, so that the crawler only follows URLs matching my criteria and avoids unwanted content.

#### Acceptance Criteria

7.1 THE URL_Filter SHALL support a configurable extension blacklist (default: .jpg, .jpeg, .png, .gif, .svg, .pdf, .zip, .exe, .mp3, .mp4, .avi) to skip non-HTML resources.

7.2 THE URL_Filter SHALL support a configurable domain whitelist — when set, only URLs from listed domains are accepted.

7.3 THE URL_Filter SHALL support a configurable domain blacklist — URLs from listed domains are rejected.

7.4 THE URL_Filter SHALL support a maximum URL length filter (default: 2048 characters).

7.5 THE URL_Filter SHALL support custom filter functions that accept a URL string and return True (accept) or False (reject).

7.6 WHEN both whitelist and blacklist are configured, THE URL_Filter SHALL apply the whitelist first (URL must be in whitelist AND not in blacklist).

7.7 Reject non-HTTP(S), missing hosts, malformed IPv6 brackets and invalid/out-of-range ports; malformed input returns False.

### Requirement 8: URL Deduplication

**User Story:** As a developer, I want URL deduplication, so that the crawler does not visit the same URL multiple times.

#### Acceptance Criteria

8.1 BEFORE enqueuing a URL into the URL_Frontier, THE Crawler SHALL check the URL_Seen_Store.

8.2 IF the URL has been seen before, THEN THE Crawler SHALL skip enqueuing it.

8.3 IF the URL has not been seen before, THEN THE Crawler SHALL add it to the URL_Seen_Store and enqueue it.

8.4 Use an in-memory set by default; state_path enables SQLite unique jobs/fingerprints and deduplication across restarts.

8.5 THE URL_Seen_Store SHALL compare URLs after normalization (lowercase scheme/host, remove fragments).

### Requirement 9: Fetcher Interface

**User Story:** As a developer, I want a pluggable fetcher interface, so that I can use custom HTTP clients or mock fetchers for testing.

#### Acceptance Criteria

9.1 THE Fetcher SHALL define an abstract interface with an async method that accepts a URL and returns the response (status code, headers, body).

9.2 THE default Fetcher implementation SHALL use `urllib.request.urlopen` wrapped in asyncio (run in executor) for HTTP fetching.

9.3 THE Fetcher interface SHALL support setting request headers (User-Agent, etc.).

9.4 THE Fetcher interface SHALL support a configurable timeout per request.

9.5 ANY implementation conforming to the Fetcher interface SHALL be usable by the HTML_Downloader without modification.

### Requirement 10: Content Processing Hooks

**User Story:** As a developer, I want a hook/plugin system for content processing, so that I can extend the crawler with custom logic (e.g., indexing, data extraction).

#### Acceptance Criteria

10.1 THE Crawler SHALL support registering Content_Hook callbacks that are invoked for each successfully crawled page.

10.2 EACH Content_Hook SHALL receive the URL, HTTP status, response headers, page content, and extracted links.

10.3 THE Crawler SHALL support multiple Content_Hooks, invoking them in registration order.

10.4 IF a Content_Hook raises an exception, THEN THE Crawler SHALL log the error and continue processing (hooks do not halt the crawl).

10.5 THE Content_Hook interface SHALL support both synchronous and asynchronous hook implementations.

### Requirement 11: Crawl Results

**User Story:** As a developer, I want structured crawl results, so that I can inspect the outcome of each crawled URL.

#### Acceptance Criteria

11.1 EACH Crawl_Result SHALL contain: the URL, HTTP status code, content length, Content_Fingerprint, list of extracted links, crawl depth, and a status indicator (success, error, duplicate, disallowed, filtered).

11.2 WHEN a page is successfully crawled, THE Crawl_Result status SHALL be "success".

11.3 WHEN a page download fails, THE Crawl_Result status SHALL be "error" with an error message.

11.4 WHEN a page is a content duplicate, THE Crawl_Result status SHALL be "duplicate".

11.5 WHEN a page is disallowed by robots.txt, THE Crawl_Result status SHALL be "disallowed".

### Requirement 12: Configuration

**User Story:** As a developer, I want a single configuration object for the crawler, so that I can easily tune crawl behavior.

#### Acceptance Criteria

12.1 THE Crawler SHALL accept a configuration object with the following settings: max_pages, max_depth, max_concurrent, per_host_delay, request_timeout, user_agent, url_filters, and content_hooks.

12.2 THE configuration SHALL provide sensible defaults: max_pages=100, max_depth=3, max_concurrent=10, per_host_delay=1.0 seconds, request_timeout=30 seconds, user_agent="WebCrawler/1.0".

12.3 THE configuration SHALL be a dataclass with type hints for all fields.

12.4 Strictly validate positive integer budgets; max_retries/max_redirects may be zero; durations must be finite and positive, and recrawling requires state_path.

### Requirement 13: Durable Scheduling, Recrawling and Routing

13.1 state_path enables SQLite with one active owner per file; process death releases the lock and startup recovers inflight jobs.
13.2 Atomically commit snapshots, fingerprints, acknowledgements and host time; retry within budget and return unacknowledged cancelled work.
13.3 External batch calls activate due recrawls; unchanged content backs off, changes restore the base interval and raise priority.
13.4 Inject ConsistentHashPartition/route_url; persist remote discoveries before delivery and require durable target acknowledgement and idempotence.
13.5 Membership, automatic safe handoff, replication/sharding, five-year retention cleanup, anti-spam models and dynamic rendering are deployment extensions.

## Non-Functional Requirements

### NFR-1: Project Structure

- **NFR-1.1**: The project SHALL use `pyproject.toml` with hatchling build system.
- **NFR-1.2**: The project SHALL use src layout (`src/web_crawler/`).
- **NFR-1.3**: The project SHALL require Python >= 3.9.
- **NFR-1.4**: The project SHALL have zero runtime dependencies (stdlib only).
- **NFR-1.5**: The project SHALL include dev dependencies: pytest, pytest-asyncio, and hypothesis.
- **NFR-1.6**: The project SHALL include a `docs/` directory for documentation.
- **NFR-1.7**: The project SHALL include an `examples/` directory with usage demonstrations.
- **NFR-1.8**: The project SHALL provide a clean `__init__.py` with public API exports.

### NFR-2: Code Quality

- **NFR-2.1**: The project SHALL include type hints throughout all public interfaces.
- **NFR-2.2**: The project SHALL include unit tests using pytest covering all public methods.
- **NFR-2.3**: The project SHALL include property-based tests using Hypothesis for URL normalization, filtering, and deduplication correctness.
- **NFR-2.4**: The project SHALL use abstract base classes (ABC) or Protocols for all pluggable interfaces.
- **NFR-2.5**: The project SHALL define a custom exceptions module for all error types.

### NFR-3: Performance

- **NFR-3.1**: URL deduplication lookup SHALL complete in O(1) average time using a hash set.
- **NFR-3.2**: Content fingerprint computation SHALL complete in O(n) time where n is the content length.
- **NFR-3.3**: Heap work is amortized logarithmic per entry; one poll may wake multiple due hosts, so strict O(log n) per call is not claimed.
- **NFR-3.4**: Concurrency is bounded by max_concurrent, host reservations and download slots; no universal no-degradation promise at 100 concurrent pages.

### NFR-4: Concurrency

- **NFR-4.1**: The crawler SHALL use asyncio for concurrent downloads.
- **NFR-4.2**: Bound task creation before semaphore-protected processing; recheck due hosts every 50 ms only with a free slot and remaining budget.
- **NFR-4.3**: The frontier belongs to the event-loop thread; blocking downloads/durable I/O use threads and drain started work on cancellation.
- **NFR-4.4**: The default fetcher wraps blocking HTTP with body/deadline/slot bounds; unresolved system DNS retains its bounded download slot.
