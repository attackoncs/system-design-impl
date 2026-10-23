"""Crawl cancellation owns and drains every child task."""
import asyncio
from types import SimpleNamespace
import pytest
from web_crawler.crawler import Crawler
from web_crawler.config import CrawlerConfig


@pytest.mark.asyncio
async def test_cancel_drains_children_and_limits_created_tasks():
    config = CrawlerConfig(max_pages=1000, max_concurrent=3, per_host_delay=0.001)
    crawler = Crawler(config, fetcher=SimpleNamespace())
    active = set()
    ready = asyncio.Event()
    async def process(entry):
        active.add(asyncio.current_task())
        if len(active) == 3:
            ready.set()
        try:
            await asyncio.Future()
        finally:
            active.remove(asyncio.current_task())
    crawler._process_url = process
    task = asyncio.create_task(crawler.crawl([f'https://host{i}.example/' for i in range(40)]))
    await asyncio.wait_for(ready.wait(), 1)
    assert len(active) == 3
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not active

