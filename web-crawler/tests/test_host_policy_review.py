"""Domain admission and politeness must use the same canonical hostname."""

import asyncio
import sqlite3
from contextlib import closing

import pytest
from web_crawler import Crawler, CrawlerConfig
from web_crawler.fetcher import FetchResponse
from web_crawler.filters import URLFilter
from web_crawler.models import CrawlResult, CrawlStatus
from web_crawler.state import SQLiteCrawlState


@pytest.mark.parametrize(
    "domain,url",
    [
        ("example.org", "https://example.org/a"),
        ("example.org", "http://user@example.org:80/a"),
        ("example.org", "http://example.org./a"),
        ("例子.中国", "http://xn--fsqu00a.xn--fiqs8s/a"),
        ("[2001:db8::1]", "http://[2001:0db8::1]:80/a"),
    ],
)
def test_domain_rules_match_hosts_without_authority_bypasses(domain, url):
    assert URLFilter(domain_whitelist=[domain]).accept(url)
    assert not URLFilter(domain_blacklist=[domain]).accept(url)


@pytest.mark.parametrize("url", ["http://%65xample.org/a", "http://example.org%3a80/a"])
def test_encoded_authority_cannot_change_the_host_after_filtering(url):
    assert not URLFilter().accept(url)


class Pages:
    def __init__(self):
        self.active = self.peak = 0

    async def fetch(self, url, **kwargs):
        if url.endswith("robots.txt"):
            return FetchResponse(200, {}, b"User-agent: *\nAllow: /", url)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            return FetchResponse(
                200, {"Content-Type": "text/html"}, f"<html>{url}</html>".encode(), url
            )
        finally:
            self.active -= 1


@pytest.mark.parametrize("durable", [False, True])
async def test_same_hostname_cannot_overlap_using_different_authorities(
    tmp_path, durable
):
    pages = Pages()
    crawler = Crawler(
        CrawlerConfig(
            per_host_delay=0.001,
            state_path=str(tmp_path / "crawl.db") if durable else None,
        ),
        pages,
    )
    results = await crawler.crawl(["http://example.org/a", "http://EXAMPLE.ORG:80/b"])
    assert len(results) == 2 and all(r.status is CrawlStatus.SUCCESS for r in results)
    assert pages.peak == 1


def test_legacy_aliases_merge_all_host_delays_before_any_dispatch(tmp_path):
    path = tmp_path / "crawl.db"
    state = SQLiteCrawlState(path)
    state.open()
    state.close()
    # Model the pre-migration schema, including a delay with no queued URL.
    with closing(sqlite3.connect(path)) as db:
        db.executemany(
            "INSERT INTO jobs(url,host,priority,depth,state) VALUES(?,?,1,0,'pending')",
            [
                ("http://example.org/a", "example.org"),
                ("http://user@example.org:80/b", "user@example.org:80"),
            ],
        )
        db.executemany(
            "INSERT INTO hosts VALUES(?,?)",
            [
                ("example.org", 110),
                ("user@example.org:80", 120),
                ("example.org.", 140),
            ],
        )
        db.execute("PRAGMA user_version=0")
        db.commit()
    now = [115.0]
    recovered = SQLiteCrawlState(path, per_host_delay=0, clock=lambda: now[0])
    recovered.open()
    try:
        assert recovered.get() is None
        now[0] = 140
        first = recovered.get()
        assert first.host == "example.org"
        assert recovered.get() is None
        recovered.finish(first, CrawlResult(first.url, CrawlStatus.DISALLOWED))
        assert recovered.get().host == "example.org"
    finally:
        recovered.close()


async def test_resumed_denied_url_is_filtered_before_partition_handoff(tmp_path):
    path = tmp_path / "crawl.db"
    state = SQLiteCrawlState(path)
    state.open()
    from web_crawler.models import FrontierEntry

    state.put(FrontierEntry("http://user@example.org:80/a"))
    state.close()
    routes = []

    class Remote:
        def owns(self, url):
            return False

        def owner_for_url(self, url):
            return "remote"

    crawler = Crawler(
        CrawlerConfig(
            state_path=str(path),
            domain_blacklist=["example.org"],
            per_host_delay=0.001,
        ),
        Pages(),
        partition=Remote(),
        route_url=lambda owner, entry: routes.append(entry.url),
    )
    results = await crawler.crawl([])
    assert [r.status for r in results] == [CrawlStatus.FILTERED]
    assert routes == []


@pytest.mark.parametrize(
    "url,host,status",
    [
        ("http://%65xample.org/a", "%65xample.org", CrawlStatus.FILTERED),
        ("http://example.org/a", "legacy custom slot", CrawlStatus.SUCCESS),
    ],
)
async def test_unsafe_legacy_host_does_not_block_recovery(tmp_path, url, host, status):
    path = tmp_path / "crawl.db"
    state = SQLiteCrawlState(path)
    state.open()
    state.close()
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "INSERT INTO jobs(url,host,priority,depth,state) VALUES(?,?,1,0,'pending')",
            (url, host),
        )
        db.execute("PRAGMA user_version=0")
        db.commit()
    pages = Pages()
    results = await Crawler(
        CrawlerConfig(state_path=str(path), per_host_delay=0.001), pages
    ).crawl([])
    assert [r.status for r in results] == [status]
    assert pages.peak == (0 if status is CrawlStatus.FILTERED else 1)
