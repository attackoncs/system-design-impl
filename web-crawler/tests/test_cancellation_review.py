"""Repeated cancellation must not abandon owned SQLite work or file locks."""

import asyncio
import threading

import pytest

from web_crawler import Crawler, CrawlerConfig
from web_crawler.fetcher import FetchResponse
from web_crawler.state import SQLiteCrawlState


class Pages:
    async def fetch(self, url, **kwargs):
        body = (
            b"User-agent: *\nAllow: /"
            if url.endswith("robots.txt")
            else b"<html>ok</html>"
        )
        return FetchResponse(200, {"content-type": "text/html"}, body, url)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["open", "finish", "close"])
async def test_repeated_cancellation_drains_database_and_releases_owner(
    tmp_path, phase
):
    path = tmp_path / "crawl.db"
    crawler = Crawler(
        CrawlerConfig(state_path=str(path), max_pages=1, per_host_delay=0.001), Pages()
    )
    entered, release, completed = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original = getattr(crawler._state, phase)

    def blocked(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(5), "test did not release database operation"
            return original(*args, **kwargs)
        finally:
            completed.set()

    setattr(crawler._state, phase, blocked)
    seeds = ["https://a.test/"] if phase == "finish" else []
    task = asyncio.create_task(crawler.crawl(seeds))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        # Allow both cancellation deliveries and the parent's cleanup to run.
        for _ in range(10):
            await asyncio.sleep(0)
        assert not task.done(), "crawl returned while its database thread still ran"
        assert crawler._running
        if phase != "open":
            other = SQLiteCrawlState(path)
            try:
                with pytest.raises(RuntimeError, match="owned"):
                    other.open()
            finally:
                other.close()
    finally:
        release.set()
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            assert await asyncio.to_thread(completed.wait, 3)
            assert not crawler._running
            assert crawler._state._lock_file is None
            # Verify automatic cleanup before the fallback close can mask a leak.
            other = SQLiteCrawlState(path)
            other.open()
            try:
                assert other.is_empty()
            finally:
                other.close()
            assert await crawler.crawl([]) == []
        finally:
            crawler._state.close()
