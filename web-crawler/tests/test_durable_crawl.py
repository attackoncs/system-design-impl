"""Durability and real pipeline regression tests without external network."""

import asyncio
import sqlite3

import pytest

from web_crawler.config import CrawlerConfig
from web_crawler.crawler import Crawler
from web_crawler.fetcher import FetchResponse
from web_crawler.models import CrawlResult, CrawlStatus, FrontierEntry
from web_crawler.state import SQLiteCrawlState


def test_recover_inflight_and_reject_second_owner(tmp_path):
    path = tmp_path / "crawl.db"
    state = SQLiteCrawlState(path, per_host_delay=0)
    state.open()
    state.put(FrontierEntry("https://a.test/page#fragment"))
    entry = state.get()
    assert entry.url == "https://a.test/page"
    other = SQLiteCrawlState(path)
    with pytest.raises(RuntimeError, match="owned"):
        other.open()
    # Closing without acknowledging models lost work, not graceful completion.
    state.close()
    other.open()
    try:
        assert other.get().url == entry.url
    finally:
        other.close()


def test_atomic_finish_persists_content_and_adaptive_recrawl(tmp_path):
    now = [100.0]
    state = SQLiteCrawlState(
        tmp_path / "crawl.db",
        per_host_delay=0,
        recrawl_interval=10,
        clock=lambda: now[0],
    )
    state.open()
    state.put(FrontierEntry("https://a.test/"))
    entry = state.get()
    result = CrawlResult(
        entry.url,
        CrawlStatus.SUCCESS,
        http_status=200,
        content_fingerprint="fingerprint",
    )
    state.finish(entry, result, "<html>original</html>")
    assert state.is_empty()
    assert state.content_seen("fingerprint")
    state.close()
    now[0] = 110
    state.open()
    entry = state.get()
    assert entry is not None
    state.finish(
        entry,
        CrawlResult(
            entry.url, CrawlStatus.DUPLICATE, content_fingerprint="fingerprint"
        ),
    )
    with state._db() as db:
        assert db.execute("SELECT due,unchanged FROM jobs").fetchone() == (130, 1)
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1
    state.close()


def test_retry_budget_is_persisted(tmp_path):
    state = SQLiteCrawlState(
        tmp_path / "crawl.db", per_host_delay=0, retry_delay=0, max_retries=1
    )
    state.open()
    try:
        state.put(FrontierEntry("https://a.test/"))
        for _ in range(2):
            entry = state.get()
            state.finish(
                entry, CrawlResult(entry.url, CrawlStatus.ERROR, error_message="failed")
            )
        assert state.is_empty()
        with state._db() as db:
            assert db.execute("SELECT attempts,last_error FROM jobs").fetchone() == (
                2,
                "failed",
            )
    finally:
        state.close()


class Pages:
    def __init__(self):
        self.calls = []
        self.body = b'<html><a href="/child">child</a></html>'

    async def fetch(self, url, **kwargs):
        self.calls.append(url)
        if url.endswith("robots.txt"):
            return FetchResponse(200, {}, b"User-agent: *\nAllow: /", url)
        return FetchResponse(200, {"content-type": "text/html"}, self.body, url)


@pytest.mark.asyncio
async def test_new_instance_resumes_pending_children_and_deduplicates_disk_content(
    tmp_path,
):
    path = tmp_path / "crawl.db"
    pages = Pages()
    config = CrawlerConfig(state_path=str(path), max_pages=1, per_host_delay=0.001)
    first = await Crawler(config, pages).crawl(["https://a.test/"])
    assert first[0].status == CrawlStatus.SUCCESS
    second = await Crawler(config, pages).crawl([])
    assert second[0].url == "https://a.test/child"
    assert second[0].status == CrawlStatus.DUPLICATE
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_cancelled_fetch_is_available_after_restart(tmp_path):
    entered = asyncio.Event()

    class Blocking(Pages):
        async def fetch(self, url, **kwargs):
            if url.endswith("robots.txt"):
                return await super().fetch(url, **kwargs)
            entered.set()
            await asyncio.Future()

    config = CrawlerConfig(
        state_path=str(tmp_path / "crawl.db"), max_pages=1, per_host_delay=0.001
    )
    crawler = Crawler(config, Blocking())
    task = asyncio.create_task(crawler.crawl(["https://a.test/"]))
    await asyncio.wait_for(entered.wait(), 2)
    with pytest.raises(RuntimeError, match="already running"):
        await crawler.crawl([])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    results = await Crawler(config, Pages()).crawl([])
    assert results[0].status == CrawlStatus.SUCCESS


@pytest.mark.asyncio
async def test_redirect_reenters_robots_and_is_not_downloaded_when_forbidden():
    class Redirect(Pages):
        async def fetch(self, url, **kwargs):
            self.calls.append(url)
            if url.endswith("robots.txt"):
                return FetchResponse(200, {}, b"User-agent: *\nDisallow: /private", url)
            if url.endswith("/start"):
                return FetchResponse(302, {"Location": "/private"}, b"", url)
            pytest.fail("forbidden redirect downloaded")

    pages = Redirect()
    results = await Crawler(CrawlerConfig(per_host_delay=0.001), pages).crawl(
        ["https://a.test/start"]
    )
    assert [r.status for r in results] == [CrawlStatus.FILTERED, CrawlStatus.DISALLOWED]


@pytest.mark.asyncio
async def test_batch_budget_bounds_failed_attempts():
    class Invalid(Pages):
        async def fetch(self, url, **kwargs):
            if url.endswith("robots.txt"):
                return await super().fetch(url, **kwargs)
            return FetchResponse(200, {}, b"not html", url)

    results = await Crawler(
        CrawlerConfig(max_pages=2, per_host_delay=0.001), Invalid()
    ).crawl([f"https://a{i}.test/" for i in range(20)])
    assert len(results) == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_concurrent": True},
        {"per_host_delay": float("nan")},
        {"request_timeout": float("inf")},
        {"max_retries": -1},
        {"recrawl_interval_seconds": 1},
    ],
)
def test_invalid_runtime_config_rejected(kwargs):
    with pytest.raises(ValueError):
        CrawlerConfig(**kwargs)


@pytest.mark.asyncio
async def test_duplicate_html_still_discovers_relative_links_for_each_base():
    pages = Pages()
    pages.body = b'<html><a href="child">child</a></html>'
    results = await Crawler(
        CrawlerConfig(per_host_delay=0.001, max_depth=1), pages
    ).crawl(["https://a.test/a/index", "https://a.test/b/index"])
    assert "https://a.test/a/child" in pages.calls
    assert "https://a.test/b/child" in pages.calls
    assert sum(r.status == CrawlStatus.SUCCESS for r in results) == 1


def test_ready_window_skips_delayed_hosts_beyond_window_size(tmp_path):
    state = SQLiteCrawlState(
        tmp_path / "crawl.db", per_host_delay=100, window_size=1, clock=lambda: 100
    )
    state.open()
    try:
        state.put(FrontierEntry("https://a.test/first"))
        entry = state.get()
        state.finish(entry, CrawlResult(entry.url, CrawlStatus.DISALLOWED))
        state.put(FrontierEntry("https://a.test/wait"))
        state.put(FrontierEntry("https://z.test/ready"))
        assert state.get().url == "https://z.test/ready"
        with state._db() as db:
            assert (
                db.execute(
                    "SELECT next_ready FROM hosts WHERE host='a.test'"
                ).fetchone()[0]
                == 200
            )
    finally:
        state.close()
