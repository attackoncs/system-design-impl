"""Accepted work survives repeated cancellation and mutable caller inputs."""

import asyncio
import threading

import pytest
from notification_system import (
    Channel,
    ContactInfo,
    DuplicateNotificationError,
    NotificationConfig,
    NotificationRequest,
    NotificationService,
    NotificationTask,
    NotificationTemplate,
    RateLimitExceededError,
    SQLiteMessageQueue,
)


def job(name="job"):
    return NotificationTask(
        name, NotificationRequest("user", Channel.EMAIL, body=name), Channel.EMAIL
    )


def service(queue=None):
    app = NotificationService(NotificationConfig(workers_per_channel=1), queue=queue)
    app.contacts.set(ContactInfo("user", email="user@example.org"))
    return app


async def cancel_twice(task):
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_repeated_cancel_keeps_sqlite_work_serialized(tmp_path):
    queue = SQLiteMessageQueue(tmp_path / "jobs.db")
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        release.wait(5)

    pending = asyncio.create_task(queue._run(blocked))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await cancel_twice(pending)
        assert not pending.done()
        assert queue._io.locked()
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
    assert not queue._io.locked()


async def test_repeated_cancel_returns_committed_sqlite_claim(tmp_path, monkeypatch):
    queue = SQLiteMessageQueue(tmp_path / "jobs.db")
    await queue.enqueue(job())
    entered, release = threading.Event(), threading.Event()
    claim = queue._claim

    def blocked(channel):
        item = claim(channel)
        entered.set()
        release.wait(5)
        return item

    monkeypatch.setattr(queue, "_claim", blocked)
    pending = asyncio.create_task(queue.claim(Channel.EMAIL))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await cancel_twice(pending)
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
    assert queue.depth(Channel.EMAIL) == 1
    assert queue.in_flight(Channel.EMAIL) == 0


async def test_repeated_stop_cancellation_returns_memory_claim():
    app = service()
    started, cleaning, release = (asyncio.Event() for _ in range(3))

    class Provider:
        async def deliver(self, task):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

    await app.send(NotificationRequest("user", Channel.EMAIL, body="hello"))
    pool = app.create_worker_pool({Channel.EMAIL: Provider()})
    await pool.start()
    stopping = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        stopping = asyncio.create_task(pool.stop(graceful=False))
        await asyncio.wait_for(cleaning.wait(), 2)
        await cancel_twice(stopping)
        assert not stopping.done()
    finally:
        release.set()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        await pool.stop(graceful=False)
    assert app.queue.depth(Channel.EMAIL) == 1
    assert app.queue.in_flight(Channel.EMAIL) == 0
    assert not pool.is_running


async def test_repeated_batch_cancel_drains_all_consumer_cleanup(monkeypatch):
    app = service()
    started, cleaning, release = (asyncio.Event() for _ in range(3))
    active, finished = set(), set()

    async def send(request):
        active.add(request.body)
        if len(active) == 2:
            started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            active.remove(request.body)
            finished.add(request.body)

    monkeypatch.setattr(app, "send", send)
    pending = asyncio.create_task(
        app.send_batch(
            [NotificationRequest("user", Channel.EMAIL, body=str(i)) for i in range(2)],
            concurrency=2,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        pending.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        await cancel_twice(pending)
        assert not pending.done()
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
    assert finished == {"0", "1"} and not active


async def test_request_dicts_are_frozen_before_admission_wait():
    app = service()
    app.templates.register(
        NotificationTemplate("greeting", "Hi {name}", "Hello {name}")
    )
    params, metadata = {"name": "Alice"}, {"label": "original"}
    request = NotificationRequest(
        "user",
        Channel.EMAIL,
        template_id="greeting",
        template_params=params,
        metadata=metadata,
    )
    lock = app._admission[hash("user") % len(app._admission)]
    await lock.acquire()
    pending = asyncio.create_task(app.send(request))
    await asyncio.sleep(0)
    params["name"], metadata["label"] = "Changed", object()
    lock.release()
    await pending
    item = await app.queue.claim(Channel.EMAIL)
    try:
        assert (item.request.title, item.request.body) == ("Hi Alice", "Hello Alice")
        assert item.request.template_params == {"name": "Alice"}
        assert item.request.metadata == {"label": "original"}
    finally:
        await app.queue.ack(item)


async def test_batch_waits_for_slow_cleanup_after_another_consumer_exits(monkeypatch):
    app = service()
    started, fast_done, release = (asyncio.Event() for _ in range(3))
    active, finished = set(), set()

    async def send(request):
        active.add(request.body)
        if len(active) == 2:
            started.set()
        try:
            await asyncio.Event().wait()
        finally:
            if request.body == "slow":
                await release.wait()
            else:
                fast_done.set()
            finished.add(request.body)

    monkeypatch.setattr(app, "send", send)
    pending = asyncio.create_task(
        app.send_batch(
            [
                NotificationRequest("user", Channel.EMAIL, body="fast"),
                NotificationRequest("user", Channel.EMAIL, body="slow"),
            ],
            concurrency=2,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        pending.cancel()
        await asyncio.wait_for(fast_done.wait(), 2)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(pending), 0.02)
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
    assert finished == {"fast", "slow"}


async def test_relative_database_path_stays_bound_after_chdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    queue = SQLiteMessageQueue("jobs.db")
    await queue.enqueue(job())
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    empty = SQLiteMessageQueue("jobs.db")
    assert queue.depth(Channel.EMAIL) == 1
    assert empty.depth(Channel.EMAIL) == 0


def test_temporary_database_paths_are_rejected():
    for path in ("", ":memory:"):
        with pytest.raises(ValueError, match="persistent"):
            SQLiteMessageQueue(path)


async def test_cancelled_admission_finishes_dedup_and_quota(monkeypatch):
    app = service()
    app._config.global_rate_limit.max_count = 1
    entered, release = asyncio.Event(), asyncio.Event()
    enqueue = app.queue.enqueue

    async def blocked(item):
        await enqueue(item)
        entered.set()
        await release.wait()

    monkeypatch.setattr(app.queue, "enqueue", blocked)
    request = NotificationRequest("user", Channel.EMAIL, body="accepted")
    pending = asyncio.create_task(app.send(request))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await cancel_twice(pending)
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
    assert app.queue.depth(Channel.EMAIL) == 1
    with pytest.raises((DuplicateNotificationError, RateLimitExceededError)):
        await app.send(request)
    with pytest.raises(RateLimitExceededError):
        await app.send(NotificationRequest("user", Channel.EMAIL, body="another"))
