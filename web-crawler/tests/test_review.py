import sqlite3
import pytest
from web_crawler.frontier import URLFrontier
from web_crawler.models import FrontierEntry, Priority
from web_crawler.archive import SQLiteArchive
from web_crawler.crawler import Crawler
from web_crawler.config import CrawlerConfig
from web_crawler.models import CrawlResult, CrawlStatus


def test_reserved_host_blocks_overlap_and_priority_does_not_leave_stale_entries():
    frontier = URLFrontier(per_host_delay=0)
    low = FrontierEntry("https://a.test/low", Priority.LOW)
    high = FrontierEntry("https://a.test/high", Priority.HIGH)
    other = FrontierEntry("https://b.test/")
    for entry in (low, high, other):
        frontier.put(entry)
    assert frontier.get(reserve=True) == high
    assert frontier.get(reserve=True) == other
    assert frontier.get(reserve=True) is None
    frontier.complete(high)
    assert frontier.get(reserve=True) == low
    assert not frontier._back_queues


@pytest.mark.asyncio
async def test_callable_async_archive_is_awaited_and_persists(tmp_path):
    path = tmp_path / "archive.db"
    crawler = Crawler(CrawlerConfig(content_hooks=[SQLiteArchive(path)]))
    await crawler._invoke_hooks(
        CrawlResult("https://a.test", CrawlStatus.SUCCESS, http_status=200),
        "<html>saved</html>",
    )
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT html FROM snapshots").fetchone()[0]
            == "<html>saved</html>"
        )
