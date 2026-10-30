"""Regression coverage for delayed claims on the supported Python versions."""

from types import SimpleNamespace

import pytest

from news_feed_system.models import FanoutTask
import news_feed_system.queue as queue_module


@pytest.mark.asyncio
async def test_delayed_claim_accepts_pre_311_asyncio_timeout(monkeypatch):
    """Python 3.9/3.10 asyncio timeouts are not builtin TimeoutError instances."""

    class LegacyAsyncioTimeoutError(Exception):
        pass

    now = 100.0
    waits = []

    async def wait_for(awaitable, timeout):
        nonlocal now
        # Advance the queue's clock to the scheduled retry without sleeping.
        # Close Event.wait's coroutine because this fake timer expires first.
        awaitable.close()
        waits.append(timeout)
        now += timeout
        raise LegacyAsyncioTimeoutError()

    queue = queue_module.MessageQueue(max_size=1)
    monkeypatch.setattr(queue_module, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(
        queue_module,
        "asyncio",
        SimpleNamespace(wait_for=wait_for, TimeoutError=LegacyAsyncioTimeoutError),
    )
    await queue.enqueue(FanoutTask("post", "author", ["reader"]))
    first = await queue.claim()
    queue.retry(first, delay_seconds=5)

    claim = await queue.claim()

    assert waits == [5]
    assert claim.task.post_id == "post"
    assert claim.attempts == 2
    assert queue.outstanding() == 1
    queue.ack(claim)
    await queue.join()
