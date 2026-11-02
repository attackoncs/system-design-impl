"""Chapter 10 regressions for host scheduling and robust link discovery."""

import random
import asyncio

import pytest

from web_crawler import frontier as frontier_module
from web_crawler.frontier import URLFrontier
from web_crawler.filters import URLFilter
from web_crawler.models import FrontierEntry, Priority
from web_crawler.parser import LinkExtractor, normalize_url


def test_busy_host_backlog_is_not_scanned_per_poll(monkeypatch):
    monkeypatch.setattr(frontier_module.time, "monotonic", lambda: 100.0)
    frontier = URLFrontier(per_host_delay=1)
    active = FrontierEntry("https://busy.test/active", Priority.HIGH)
    frontier.put(active)
    assert frontier.get(reserve=True) is active
    for index in range(2000):
        frontier.put(FrontierEntry(f"https://busy.test/{index}", Priority.HIGH))
    ready = FrontierEntry("https://ready.test/", Priority.LOW)
    frontier.put(ready)
    pops = 0
    original = frontier_module.heapq.heappop

    def counted(heap):
        nonlocal pops
        pops += 1
        return original(heap)

    monkeypatch.setattr(frontier_module.heapq, "heappop", counted)
    assert frontier.get(reserve=True) is ready
    assert frontier.get(reserve=True) is None
    assert frontier.size == 2000
    assert pops < 20, "polling must not traverse every URL of a reserved host"


def test_first_request_is_ready_even_at_monotonic_zero(monkeypatch):
    monkeypatch.setattr(frontier_module.time, "monotonic", lambda: 0.0)
    frontier = URLFrontier(per_host_delay=10)
    entry = FrontierEntry("https://example.test/")
    frontier.put(entry)
    assert frontier.get(reserve=True) is entry


def test_delay_starts_on_completion_and_applies_to_late_enqueue(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(frontier_module.time, "monotonic", lambda: now[0])
    frontier = URLFrontier(per_host_delay=2)
    first, second = (FrontierEntry(f"https://a.test/{i}") for i in range(2))
    frontier.put(first)
    assert frontier.get(reserve=True) is first
    now[0] = 110.0
    frontier.complete(first)
    frontier.put(second)
    now[0] = 111.9
    assert frontier.get(reserve=True) is None
    now[0] = 112.0
    assert frontier.get(reserve=True) is second


def test_priority_change_and_equal_priority_fifo(monkeypatch):
    monkeypatch.setattr(frontier_module.time, "monotonic", lambda: 100.0)
    frontier = URLFrontier(per_host_delay=0)
    entries = [
        FrontierEntry("https://a.test/low", Priority.LOW),
        FrontierEntry("https://b.test/medium", Priority.MEDIUM),
        FrontierEntry("https://a.test/high", Priority.HIGH),
        FrontierEntry("https://b.test/high", Priority.HIGH),
        FrontierEntry("https://a.test/medium", Priority.MEDIUM),
    ]
    for entry in entries:
        frontier.put(entry)
    assert [frontier.get() for _ in entries] == [entries[i] for i in (2, 3, 1, 4, 0)]
    assert frontier.get() is None
    assert frontier.is_empty()


@pytest.mark.parametrize("seed", range(8))
def test_frontier_matches_simple_scheduling_oracle(monkeypatch, seed):
    """Compare with a full scan oracle, including reservations and fake time."""
    now = [100.0]
    monkeypatch.setattr(frontier_module.time, "monotonic", lambda: now[0])
    frontier = URLFrontier(per_host_delay=1)
    rng = random.Random(seed)
    pending, active, last = [], {}, {}
    for sequence in range(500):
        now[0] += rng.choice((0, 0.1, 1.0))
        action = rng.randrange(4)
        if action < 2:
            entry = FrontierEntry(
                f"https://h{rng.randrange(5)}.test/{sequence}",
                rng.choice(list(Priority)),
            )
            pending.append((entry.priority.value, sequence, entry))
            frontier.put(entry)
        elif action == 2:
            reserve = bool(rng.randrange(2))
            eligible = [
                item
                for item in pending
                if item[2].host not in active
                and now[0] - last.get(item[2].host, float("-inf")) >= 1
            ]
            selected = min(eligible, default=None, key=lambda item: item[:2])
            actual = frontier.get(reserve=reserve)
            assert actual is (selected[2] if selected else None)
            if selected:
                pending.remove(selected)
                last[actual.host] = now[0]
                if reserve:
                    active[actual.host] = actual
        elif active:
            host = rng.choice(list(active))
            frontier.complete(active.pop(host))
            last[host] = now[0]
        assert frontier.size == len(pending)
        assert frontier.is_empty() == (not pending)


def test_malformed_link_does_not_discard_valid_siblings():
    assert LinkExtractor().extract(
        '<a href="http://[bad">bad</a><a href="child">valid</a>',
        "https://example.org/dir/index",
    ) == ["https://example.org/dir/child"]


@pytest.mark.parametrize(
    "url", ["https://[bad", "https://:80/", "http:///path", "https://a.test:bad/"]
)
def test_malformed_seed_url_is_filtered(url):
    assert URLFilter().accept(url) is False


def test_first_base_href_controls_relative_links():
    assert LinkExtractor().extract(
        '<a href="child#section">link</a><base href="/assets/">'
        '<base href="https://ignored.test/">',
        "https://example.org/dir/index",
    ) == ["https://example.org/assets/child"]


def test_bad_base_href_falls_back_to_document_url():
    assert LinkExtractor().extract(
        '<base href="https://[bad"><a href="child">link</a>',
        "https://example.org/dir/index",
    ) == ["https://example.org/dir/child"]


def test_normalization_preserves_case_sensitive_credentials():
    assert normalize_url("HTTPS://User:Pass@EXAMPLE.ORG/Path?Case=YES#frag") == (
        "https://User:Pass@example.org/Path?Case=YES"
    )
    assert normalize_url("https://user:PASS@example.org") != normalize_url(
        "https://user:pass@example.org"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,allowed",
    [
        ("/rss/people/alice/reviews", False),
        ("/private/nested/file", False),
        ("/private/public/item", True),
        ("/private/equal", True),
        ("/ending.pdf", False),
        ("/ending.pdf?download=1", True),
        ("/normal", True),
    ],
)
async def test_book_robots_patterns_and_longest_allow_rule(path, allowed):
    from web_crawler.fetcher import FetchResponse
    from web_crawler.robots import RobotsChecker

    class Robots:
        async def fetch(self, url, **kwargs):
            return FetchResponse(
                200,
                {},
                (
                    b"User-agent: *\nDisallow: /rss/people/*/reviews\n"
                    b"Disallow: /private/*\nAllow: /private/public/\n"
                    b"Disallow: /private/equal\nAllow: /private/equal\n"
                    b"Disallow: /*.pdf$\n"
                ),
                url,
            )

    checker = RobotsChecker(Robots())
    assert await checker.is_allowed("https://example.org" + path) is allowed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rule,path,allowed",
    [
        ("/private%23secret", "/private%23secret", False),
        ("/", "?q=1", False),
        ("/?q=", "?q=1", False),
        ("/literal%2Aend", "/literalANYend", True),
        ("/literal%2Aend", "/literal*end", False),
        ("/price%24", "/price$", False),
        ("/price%24", "/price", True),
        ("/encoded%2Fchild", "/encoded/child", True),
        ("/encoded%2Fchild", "/encoded%2fchild", False),
        ("/unreserved%7E", "/unreserved~", False),
        ("/unreserved~", "/unreserved%7e", False),
        ("/你好", "/%E4%BD%A0%E5%A5%BD", False),
    ],
)
async def test_robots_preserves_encoded_delimiters_and_literal_metacharacters(
    rule, path, allowed
):
    from web_crawler.fetcher import FetchResponse
    from web_crawler.robots import RobotsChecker

    class Robots:
        async def fetch(self, url, **kwargs):
            body = f"User-agent: *\nDisallow: {rule}\n".encode("utf-8")
            return FetchResponse(200, {}, body, url)

    assert (
        await RobotsChecker(Robots()).is_allowed("https://example.org" + path)
        is allowed
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_pipeline_keeps_valid_links_and_document_base(tmp_path, durable):
    from web_crawler import Crawler, CrawlerConfig
    from web_crawler.fetcher import FetchResponse
    from web_crawler.models import CrawlStatus

    class Pages:
        def __init__(self):
            self.calls = []

        async def fetch(self, url, **kwargs):
            self.calls.append(url)
            if url.endswith("robots.txt"):
                return FetchResponse(200, {}, b"User-agent: *\nAllow: /", url)
            body = (
                b'<html><base href="/assets/"><a href="http://[bad">bad</a>'
                b'<a href="child">valid</a></html>'
                if url.endswith("/index")
                else b"<html>child</html>"
            )
            return FetchResponse(200, {"Content-Type": "text/html"}, body, url)

    pages = Pages()
    results = await Crawler(
        CrawlerConfig(
            per_host_delay=0.001,
            max_pages=2,
            state_path=str(tmp_path / "crawl.db") if durable else None,
        ),
        pages,
    ).crawl(["https://example.org/index"])
    assert [(result.url, result.status) for result in results] == [
        ("https://example.org/index", CrawlStatus.SUCCESS),
        ("https://example.org/assets/child", CrawlStatus.SUCCESS),
    ]
    assert "https://example.org/assets/child" in pages.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_idle_slot_rechecks_host_deadline_while_other_download_is_slow(
    tmp_path, durable
):
    from web_crawler import Crawler, CrawlerConfig
    from web_crawler.fetcher import FetchResponse
    from web_crawler.models import CrawlStatus

    child_started = asyncio.Event()

    class Pages:
        async def fetch(self, url, **kwargs):
            if url.endswith("robots.txt"):
                return FetchResponse(200, {}, b"User-agent: *\nAllow: /", url)
            if "slow.test" in url:
                await child_started.wait()
                body = b"<html>slow host</html>"
            elif url.endswith("/start"):
                body = b'<html><a href="/child">next</a></html>'
            else:
                child_started.set()
                body = b"<html>fast host child</html>"
            return FetchResponse(200, {"Content-Type": "text/html"}, body, url)

    crawler = Crawler(
        CrawlerConfig(
            max_concurrent=2,
            max_pages=3,
            per_host_delay=0.05,
            state_path=str(tmp_path / "crawl.db") if durable else None,
        ),
        Pages(),
    )
    results = await asyncio.wait_for(
        crawler.crawl(
            [
                "https://slow.test/start",
                "https://fast.test/start",
            ]
        ),
        timeout=2,
    )
    assert child_started.is_set()
    assert len(results) == 3
    assert all(result.status == CrawlStatus.SUCCESS for result in results)
