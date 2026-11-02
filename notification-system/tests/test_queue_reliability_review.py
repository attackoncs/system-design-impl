"""Receipt isolation and lease clocks under actual SQLite write contention."""

import asyncio
from contextlib import closing, contextmanager
import sqlite3
import threading
import time

import pytest

from notification_system import (
    Channel,
    MessageQueue,
    NotificationRequest,
    NotificationTask,
    SQLiteMessageQueue,
)


def task():
    return NotificationTask(
        "job", NotificationRequest("user", Channel.EMAIL, body="hello"), Channel.EMAIL
    )


@pytest.mark.parametrize("operation", ["ack", "reschedule", "renew"])
@pytest.mark.asyncio
async def test_memory_old_claim_cannot_control_reclaimed_job(operation):
    queue = MessageQueue(max_depth=1)
    await queue.enqueue(task())
    stale = await queue.claim(Channel.EMAIL)
    stale_receipt = stale.receipt
    await queue.reschedule(stale)
    current = await queue.claim(Channel.EMAIL)

    if operation == "renew":
        assert not await queue.renew(stale)
    else:
        with pytest.raises(RuntimeError, match="already released"):
            await getattr(queue, operation)(stale)
    assert stale.receipt == stale_receipt
    assert current.receipt != stale_receipt
    assert await queue.renew(current)
    assert queue.in_flight(Channel.EMAIL) == 1
    await queue.ack(current)


async def contended_call(queue, operation, clock, monkeypatch):
    """Advance the clock while an operation waits behind a real write lock."""
    entered = threading.Event()
    connection = queue._db.connection

    @contextmanager
    def observed_connection():
        with connection() as db:
            db.set_trace_callback(
                lambda statement: (
                    entered.set()
                    if statement.upper().startswith(
                        ("BEGIN IMMEDIATE", "UPDATE NOTIFICATION_JOBS")
                    )
                    else None
                )
            )
            yield db

    monkeypatch.setattr(queue._db, "connection", observed_connection)
    with closing(sqlite3.connect(queue._db.path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        pending = asyncio.create_task(operation())
        try:
            assert await asyncio.to_thread(entered.wait, 2), (
                "SQLite operation did not start"
            )
            clock[0] += queue.lease_seconds * 2
        finally:
            writer.rollback()
        return await asyncio.wait_for(pending, 2)


@pytest.mark.asyncio
async def test_sqlite_claim_lease_starts_after_writer_wait(tmp_path, monkeypatch):
    queue = SQLiteMessageQueue(tmp_path / "claim.db", lease_seconds=10)
    await queue.enqueue(task())
    clock = [time.time()]
    monkeypatch.setattr("notification_system.durable.time.time", lambda: clock[0])
    claimed = await contended_call(
        queue, lambda: queue.claim(Channel.EMAIL), clock, monkeypatch
    )
    assert queue.in_flight(Channel.EMAIL) == 1
    await queue.ack(claimed)


@pytest.mark.asyncio
async def test_sqlite_renew_cannot_resurrect_lease_expired_during_writer_wait(
    tmp_path, monkeypatch
):
    queue = SQLiteMessageQueue(tmp_path / "renew.db", lease_seconds=10)
    await queue.enqueue(task())
    claimed = await queue.claim(Channel.EMAIL)
    clock = [time.time()]
    monkeypatch.setattr("notification_system.durable.time.time", lambda: clock[0])
    renewed = await contended_call(
        queue, lambda: queue.renew(claimed), clock, monkeypatch
    )
    assert not renewed
    recovered = await queue.claim(Channel.EMAIL)
    assert recovered.receipt != claimed.receipt
    with pytest.raises(RuntimeError, match="lease was lost"):
        await queue.ack(claimed)
    await queue.ack(recovered)
