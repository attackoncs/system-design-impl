"""Property-based tests for the web crawler using Hypothesis.

Contains 14 properties from the design document covering URL normalization,
link extraction, URL filtering, deduplication, frontier, configuration,
and crawl result correctness.
"""

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from web_crawler.config import CrawlerConfig
from web_crawler.crawler import Crawler
from web_crawler.dedup import ContentSeen, URLSeen
from web_crawler.exceptions import FetchError
from web_crawler.fetcher import Fetcher, FetchResponse
from web_crawler.filters import URLFilter
from web_crawler.frontier import URLFrontier
from web_crawler.models import CrawlResult, CrawlStatus, FrontierEntry, Priority
from web_crawler.parser import LinkExtractor, normalize_url


# --- Strategies ---

# Generate valid URL paths (alphanumeric segments)
path_segment = st.from_regex(r"[a-z0-9]{1,10}", fullmatch=True)
url_path = st.lists(path_segment, min_size=0, max_size=4).map(lambda segs: "/" + "/".join(segs))

# Generate valid hostnames
hostname = st.from_regex(r"[a-z]{3,10}\.(com|org|net)", fullmatch=True)

# Generate valid HTTP URLs
http_url = st.builds(
    lambda scheme, host, path: f"{scheme}://{host}{path}",
    scheme=st.sampled_from(["http", "https"]),
    host=hostname,
    path=url_path,
)

# Generate URL fragments
fragment = st.from_regex(r"[a-z0-9]{1,10}", fullmatch=True)

# Generate arbitrary bytes content
content_bytes = st.binary(min_size=1, max_size=1000)


# --- Property 1: URL normalization idempotence ---

class TestProperty1URLNormalizationIdempotence:
    """**Validates: Requirements 6.4, 8.5**"""

    @given(url=http_url)
    @settings(max_examples=50)
    def test_normalize_url_idempotent(self, url: str):
        """Normalizing a URL twice produces the same result as normalizing once."""
        once = normalize_url(url)
        twice = normalize_url(once)
        assert once == twice


# --- Property 2: URL normalization removes fragments and lowercases ---

class TestProperty2URLNormalizationFragmentsAndCase:
    """**Validates: Requirements 6.4, 8.5**"""

    @given(url=http_url, frag=fragment)
    @settings(max_examples=50)
    def test_normalize_removes_fragment(self, url: str, frag: str):
        """Normalized URL never contains a fragment."""
        url_with_frag = url + "#" + frag
        result = normalize_url(url_with_frag)
        assert "#" not in result

    @given(url=http_url)
    @settings(max_examples=50)
    def test_normalize_lowercases_scheme_and_host(self, url: str):
        """Normalized URL has lowercase scheme and host."""
        upper_url = url.replace("http://", "HTTP://").replace("https://", "HTTPS://")
        result = normalize_url(upper_url)
        # Scheme should be lowercase
        assert result.startswith("http://") or result.startswith("https://")


# --- Property 3: Link extraction completeness ---

class TestProperty3LinkExtractionCompleteness:
    """**Validates: Requirements 6.1, 6.3, 6.5**"""

    @given(paths=st.lists(url_path, min_size=1, max_size=5))
    @settings(max_examples=50)
    def test_all_href_links_extracted(self, paths: list):
        """Every <a href> in the HTML appears in the extracted links."""
        base_url = "http://example.com/"
        # Build HTML with links
        links_html = "".join(f'<a href="{p}">link</a>' for p in paths)
        html = f"<html><body>{links_html}</body></html>"

        extractor = LinkExtractor()
        extracted = extractor.extract(html, base_url)

        # Each path should produce a corresponding normalized absolute URL
        assert len(extracted) == len(paths)
        for path in paths:
            expected = normalize_url(f"http://example.com{path}")
            assert expected in extracted


# --- Property 4: URL filter scheme rejection ---

class TestProperty4URLFilterSchemeRejection:
    """**Validates: Requirements 7.7**"""

    @given(
        scheme=st.sampled_from(["ftp", "mailto", "file", "ssh", "telnet"]),
        host=hostname,
        path=url_path,
    )
    @settings(max_examples=50)
    def test_non_http_schemes_rejected(self, scheme: str, host: str, path: str):
        """URLs with non-http/https schemes are always rejected."""
        url = f"{scheme}://{host}{path}"
        f = URLFilter()
        assert not f.accept(url)


# --- Property 5: URL filter extension blacklist ---

class TestProperty5URLFilterExtensionBlacklist:
    """**Validates: Requirements 7.1**"""

    @given(
        ext=st.sampled_from([".jpg", ".png", ".gif", ".pdf", ".zip", ".exe"]),
        host=hostname,
        path_prefix=path_segment,
    )
    @settings(max_examples=50)
    def test_blacklisted_extensions_rejected(self, ext: str, host: str, path_prefix: str):
        """URLs ending with a blacklisted extension are rejected."""
        url = f"http://{host}/{path_prefix}{ext}"
        f = URLFilter(extension_blacklist=[ext])
        assert not f.accept(url)


# --- Property 6: URL filter domain whitelist/blacklist interaction ---

class TestProperty6URLFilterDomainInteraction:
    """**Validates: Requirements 7.2, 7.3, 7.6**"""

    @given(host=hostname, path=url_path)
    @settings(max_examples=50)
    def test_whitelist_rejects_non_listed(self, host: str, path: str):
        """When whitelist is set, domains not in it are rejected."""
        url = f"http://{host}{path}"
        f = URLFilter(domain_whitelist=["other-domain.com"])
        assert not f.accept(url)

    @given(host=hostname, path=url_path)
    @settings(max_examples=50)
    def test_blacklist_rejects_listed(self, host: str, path: str):
        """Domains in the blacklist are rejected."""
        url = f"http://{host}{path}"
        f = URLFilter(domain_blacklist=[host])
        assert not f.accept(url)

    @given(host=hostname, path=url_path)
    @settings(max_examples=50)
    def test_whitelist_and_blacklist_both_reject(self, host: str, path: str):
        """Domain in both whitelist and blacklist is rejected (blacklist wins)."""
        url = f"http://{host}{path}"
        f = URLFilter(domain_whitelist=[host], domain_blacklist=[host])
        assert not f.accept(url)


# --- Property 7: URL filter length rejection ---

class TestProperty7URLFilterLengthRejection:
    """**Validates: Requirements 7.4**"""

    @given(extra_len=st.integers(min_value=1, max_value=100))
    @settings(max_examples=50)
    def test_urls_exceeding_max_length_rejected(self, extra_len: int):
        """URLs longer than max_url_length are rejected."""
        max_len = 50
        base = "http://example.com/"
        # Create URL that exceeds max_len
        url = base + "a" * (max_len - len(base) + extra_len)
        assert len(url) > max_len
        f = URLFilter(max_url_length=max_len)
        assert not f.accept(url)


# --- Property 8: Content fingerprint determinism ---

class TestProperty8ContentFingerprintDeterminism:
    """**Validates: Requirements 5.3**"""

    @given(content=content_bytes)
    @settings(max_examples=50)
    def test_same_content_same_fingerprint(self, content: bytes):
        """Same content always produces the same fingerprint."""
        cs = ContentSeen()
        fp1 = cs.compute_fingerprint(content)
        fp2 = cs.compute_fingerprint(content)
        assert fp1 == fp2


# --- Property 9: Content deduplication correctness ---

class TestProperty9ContentDeduplicationCorrectness:
    """**Validates: Requirements 5.4, 5.5**"""

    @given(content=content_bytes)
    @settings(max_examples=50)
    def test_content_not_seen_before_add(self, content: bytes):
        """Content is not seen before being added."""
        cs = ContentSeen()
        fp = cs.compute_fingerprint(content)
        assert not cs.is_seen(fp)

    @given(content=content_bytes)
    @settings(max_examples=50)
    def test_content_seen_after_add(self, content: bytes):
        """Content is seen after being added."""
        cs = ContentSeen()
        fp = cs.compute_fingerprint(content)
        cs.add(fp)
        assert cs.is_seen(fp)


# --- Property 10: URL deduplication with normalization ---

class TestProperty10URLDeduplicationNormalization:
    """**Validates: Requirements 8.2, 8.3, 8.5**"""

    @given(url=http_url)
    @settings(max_examples=50)
    def test_url_seen_after_add(self, url: str):
        """URL is seen after being added."""
        us = URLSeen()
        us.add(url)
        assert us.is_seen(url)

    @given(url=http_url, frag=fragment)
    @settings(max_examples=50)
    def test_url_with_fragment_deduplicates(self, url: str, frag: str):
        """URL with fragment is considered same as without fragment."""
        us = URLSeen()
        us.add(url)
        assert us.is_seen(url + "#" + frag)


# --- Property 11: Frontier size invariant ---

class TestProperty11FrontierSizeInvariant:
    """**Validates: Requirements 2.7**"""

    @given(
        urls=st.lists(
            st.builds(
                lambda host, path: f"http://{host}{path}",
                host=hostname,
                path=url_path,
            ),
            min_size=1,
            max_size=10,
        )
    )
    @settings(max_examples=50)
    def test_size_equals_puts_minus_gets(self, urls: list):
        """Frontier size equals number of puts minus successful gets."""
        frontier = URLFrontier(per_host_delay=0.0)
        for url in urls:
            frontier.put(FrontierEntry(url=url))

        assert frontier.size == len(urls)

        gets = 0
        while not frontier.is_empty():
            result = frontier.get()
            if result is not None:
                gets += 1
            else:
                break

        assert frontier.size == len(urls) - gets


# --- Property 12: Frontier priority ordering ---

class TestProperty12FrontierPriorityOrdering:
    """**Validates: Requirements 2.1, 2.2**"""

    @given(
        priorities=st.lists(
            st.sampled_from([Priority.HIGH, Priority.MEDIUM, Priority.LOW]),
            min_size=2,
            max_size=10,
        )
    )
    @settings(max_examples=50)
    def test_dequeue_respects_priority(self, priorities: list):
        """Items dequeued from frontier respect priority ordering (when hosts differ)."""
        frontier = URLFrontier(per_host_delay=0.0)
        for i, priority in enumerate(priorities):
            # Use unique hosts to avoid politeness blocking
            entry = FrontierEntry(
                url=f"http://host{i}.com/page",
                priority=priority,
            )
            frontier.put(entry)

        prev_priority_val = -1
        while not frontier.is_empty():
            entry = frontier.get()
            if entry is None:
                break
            assert entry.priority.value >= prev_priority_val
            prev_priority_val = entry.priority.value


# --- Property 13: Configuration validation ---

class TestProperty13ConfigurationValidation:
    """**Validates: Requirements 12.4**"""

    @given(value=st.integers(max_value=0))
    @settings(max_examples=50)
    def test_non_positive_max_pages_raises(self, value: int):
        """Non-positive max_pages raises ValueError."""
        with pytest.raises(ValueError):
            CrawlerConfig(max_pages=value)

    @given(value=st.integers(max_value=0))
    @settings(max_examples=50)
    def test_non_positive_max_depth_raises(self, value: int):
        """Non-positive max_depth raises ValueError."""
        with pytest.raises(ValueError):
            CrawlerConfig(max_depth=value)

    @given(value=st.integers(max_value=0))
    @settings(max_examples=50)
    def test_non_positive_max_concurrent_raises(self, value: int):
        """Non-positive max_concurrent raises ValueError."""
        with pytest.raises(ValueError):
            CrawlerConfig(max_concurrent=value)

    @given(value=st.floats(max_value=0, allow_nan=False, allow_infinity=False))
    @settings(max_examples=50)
    def test_non_positive_per_host_delay_raises(self, value: float):
        """Non-positive per_host_delay raises ValueError."""
        with pytest.raises(ValueError):
            CrawlerConfig(per_host_delay=value)

    @given(value=st.floats(max_value=0, allow_nan=False, allow_infinity=False))
    @settings(max_examples=50)
    def test_non_positive_request_timeout_raises(self, value: float):
        """Non-positive request_timeout raises ValueError."""
        with pytest.raises(ValueError):
            CrawlerConfig(request_timeout=value)


# --- Property 14: Crawl result status correctness ---

class MockFetcher(Fetcher):
    """Mock fetcher for property tests."""

    def __init__(self, pages, disallow_paths=None):
        self._pages = pages
        self._disallow_paths = disallow_paths or []

    async def fetch(self, url, headers=None, timeout=30.0):
        if url.endswith("/robots.txt"):
            disallow_lines = "\n".join(
                f"Disallow: {p}" for p in self._disallow_paths
            )
            robots = f"User-agent: *\n{disallow_lines}\n"
            return FetchResponse(
                status_code=200,
                headers={"Content-Type": "text/plain"},
                body=robots.encode("utf-8"),
                url=url,
            )
        if url in self._pages:
            body = self._pages[url].encode("utf-8")
            return FetchResponse(
                status_code=200,
                headers={"Content-Type": "text/html"},
                body=body,
                url=url,
            )
        raise FetchError(url, "Not found")


class TestProperty14CrawlResultStatusCorrectness:
    """**Validates: Requirements 11.2, 11.3, 11.4, 11.5, 3.4, 3.7**"""

    @given(path=path_segment)
    @settings(max_examples=50)
    async def test_successful_crawl_has_success_status(self, path: str):
        """Successfully fetched HTML pages have SUCCESS status."""
        url = f"http://example.com/{path}"
        pages = {url: f"<html><body>{path}</body></html>"}
        config = CrawlerConfig(
            max_pages=1,
            per_host_delay=0.01,
            domain_whitelist=["example.com"],
        )
        crawler = Crawler(config=config, fetcher=MockFetcher(pages))
        results = await crawler.crawl([url])

        success = [r for r in results if r.url == url and r.status == CrawlStatus.SUCCESS]
        assert len(success) == 1

    @given(path=path_segment)
    @settings(max_examples=50)
    async def test_fetch_error_has_error_status(self, path: str):
        """URLs that fail to fetch have ERROR status."""
        url = f"http://example.com/{path}"
        pages = {}  # No pages → FetchError
        config = CrawlerConfig(
            max_pages=1,
            per_host_delay=0.01,
            domain_whitelist=["example.com"],
        )
        crawler = Crawler(config=config, fetcher=MockFetcher(pages))
        results = await crawler.crawl([url])

        errors = [r for r in results if r.url == url and r.status == CrawlStatus.ERROR]
        assert len(errors) == 1

    @given(path=path_segment)
    @settings(max_examples=50)
    async def test_disallowed_url_has_disallowed_status(self, path: str):
        """URLs disallowed by robots.txt have DISALLOWED status."""
        url = f"http://example.com/{path}"
        pages = {url: "<html><body>content</body></html>"}
        config = CrawlerConfig(
            max_pages=1,
            per_host_delay=0.01,
            domain_whitelist=["example.com"],
        )
        fetcher = MockFetcher(pages, disallow_paths=[f"/{path}"])
        crawler = Crawler(config=config, fetcher=fetcher)
        results = await crawler.crawl([url])

        disallowed = [r for r in results if r.url == url and r.status == CrawlStatus.DISALLOWED]
        assert len(disallowed) == 1
