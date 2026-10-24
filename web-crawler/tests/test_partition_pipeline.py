"""Exercise worker ownership through the real crawl pipeline without network."""

import asyncio
import sqlite3

import pytest
from web_crawler.config import CrawlerConfig
from web_crawler.crawler import Crawler
from web_crawler.fetcher import FetchResponse
from web_crawler.models import CrawlStatus
from web_crawler.partition import ConsistentHashPartition


def _url_owned_by(partition, owner):
    for index in range(1000):
        url = f"https://host-{index}.test/page"
        if partition.owner_for_url(url) == owner:
            return url
    raise AssertionError("test topology must assign a URL to each worker")


class FakePages:
    def __init__(self, body):
        self.body = body
        self.calls = []

    async def fetch(self, url, **kwargs):
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            return FetchResponse(200, {}, b"User-agent: *\nAllow: /", url)
        return FetchResponse(200, {"content-type": "text/html"}, self.body, url)


def test_partition_requires_explicit_router():
    partition = ConsistentHashPartition(["a", "b"], local_node_id="a")
    with pytest.raises(ValueError, match="route_url"):
        Crawler(partition=partition)


@pytest.mark.asyncio
async def test_discovered_remote_url_reaches_owner_without_local_fetch():
    sender_partition = ConsistentHashPartition(["a", "b"], local_node_id="a")
    receiver_partition = ConsistentHashPartition(["a", "b"], local_node_id="b")
    local_url = _url_owned_by(sender_partition, "a")
    remote_url = _url_owned_by(sender_partition, "b")
    pages = FakePages(f'<html><a href="{remote_url}">remote</a></html>'.encode())
    deliveries = []

    async def route(owner, entry):
        await asyncio.sleep(0)
        deliveries.append((owner, entry))

    config = CrawlerConfig(max_pages=10, per_host_delay=0.001)
    results = await Crawler(
        config, pages, partition=sender_partition, route_url=route
    ).crawl([local_url, remote_url])
    assert {(result.url, result.status) for result in results} == {
        (local_url, CrawlStatus.SUCCESS),
        (remote_url, CrawlStatus.FILTERED),
    }
    assert len(deliveries) == 1
    assert deliveries[0][0] == "b"
    assert deliveries[0][1].url == remote_url
    assert remote_url not in pages.calls
    assert all(sender_partition.owns(url) for url in pages.calls)

    receiver_pages = FakePages(b"<html>receiver content</html>")
    receiver_results = await Crawler(
        config, receiver_pages, partition=receiver_partition, route_url=route
    ).crawl([entry.url for owner, entry in deliveries if owner == "b"])
    assert [(result.url, result.status) for result in receiver_results] == [
        (remote_url, CrawlStatus.SUCCESS)
    ]
    assert remote_url in receiver_pages.calls


@pytest.mark.asyncio
async def test_durable_handoff_is_recorded_before_callback_and_retried(tmp_path):
    partition = ConsistentHashPartition(["a", "b"], local_node_id="a")
    remote_url = _url_owned_by(partition, "b")
    path = tmp_path / "sender.db"
    observations = []
    pages = FakePages(b"<html>must not download</html>")

    async def route(owner, entry):
        with sqlite3.connect(path) as db:
            row = db.execute(
                "SELECT state,attempts FROM jobs WHERE url=?", (entry.url,)
            ).fetchone()
        observations.append((owner, row))
        if len(observations) == 1:
            raise OSError("receiver unavailable")
        await asyncio.sleep(0)

    config = CrawlerConfig(
        state_path=str(path),
        max_pages=2,
        max_retries=2,
        retry_delay_seconds=0.001,
        per_host_delay=0.001,
    )
    results = await Crawler(config, pages, partition=partition, route_url=route).crawl(
        [remote_url]
    )
    assert [result.status for result in results] == [
        CrawlStatus.ERROR,
        CrawlStatus.FILTERED,
    ]
    assert observations == [("b", ("inflight", 1)), ("b", ("inflight", 2))]
    assert pages.calls == []
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT state,last_status FROM jobs").fetchone() == (
            "done",
            "filtered",
        )

    resumed = await Crawler(config, pages, partition=partition, route_url=route).crawl(
        []
    )
    assert resumed == []
    assert len(observations) == 2


@pytest.mark.asyncio
async def test_sync_router_is_supported_without_fetching_remote_url():
    partition = ConsistentHashPartition(["a", "b"], local_node_id="a")
    remote_url = _url_owned_by(partition, "b")
    deliveries = []
    pages = FakePages(b"<html>must not download</html>")
    results = await Crawler(
        CrawlerConfig(per_host_delay=0.001),
        pages,
        partition=partition,
        route_url=lambda owner, entry: deliveries.append((owner, entry.url)),
    ).crawl([remote_url])
    assert deliveries == [("b", remote_url)]
    assert [result.status for result in results] == [CrawlStatus.FILTERED]
    assert pages.calls == []
