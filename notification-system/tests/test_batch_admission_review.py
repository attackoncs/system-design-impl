"""Batch admission bounds task allocation, preserves order, and drains cancellation."""

import asyncio

import pytest

from notification_system.durable import SQLiteMessageQueue
from notification_system.models import Channel, ContactInfo, NotificationRequest
from notification_system.service import NotificationService


def requests(count):
    return [
        NotificationRequest(
            recipient_id=f"user-{i}", channel=Channel.EMAIL, body=str(i)
        )
        for i in range(count)
    ]


class RecordingService(NotificationService):
    def __init__(self):
        super().__init__()
        self.active = 0
        self.peak = 0
        self.children = set()

    async def send(self, request):
        self.children.add(asyncio.current_task())
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(int(request.body) % 3 / 1000)
            if request.body == "7":
                raise ValueError("one recipient rejected")
            return f"id-{request.body}"
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_default_batch_uses_bounded_workers_and_preserves_positions():
    service = RecordingService()
    result = await service.send_batch(requests(513))
    assert result == ["" if i == 7 else f"id-{i}" for i in range(513)]
    assert 1 < service.peak <= 16
    assert len(service.children) <= 16
    assert service.active == 0


@pytest.mark.asyncio
async def test_batch_concurrency_one_keeps_sequential_admission_available():
    service = RecordingService()
    result = await service.send_batch(requests(16), concurrency=1)
    assert len(result) == 16 and result[7] == ""
    assert service.peak == len(service.children) == 1


@pytest.mark.asyncio
async def test_batch_cancellation_drains_all_children():
    service = NotificationService()
    started, blocked = asyncio.Event(), asyncio.Event()
    active, completed = set(), set()

    async def send(request):
        child = asyncio.current_task()
        active.add(child)
        if len(active) == 3:
            started.set()
        try:
            await blocked.wait()
        finally:
            active.remove(child)
            completed.add(child)

    service.send = send
    parent = asyncio.create_task(service.send_batch(requests(1000), concurrency=3))
    try:
        await asyncio.wait_for(started.wait(), 1)
        parent.cancel()
        with pytest.raises(asyncio.CancelledError):
            await parent
        assert not active
        assert len(completed) == 3
        assert all(child.done() for child in completed)
    finally:
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
async def test_parallel_batch_keeps_first_event_and_original_identity(
    tmp_path, persistent
):
    queue = SQLiteMessageQueue(tmp_path / "jobs.db") if persistent else None
    service = NotificationService(queue=queue)
    service.contacts.set(ContactInfo(user_id="user", email="user@example.org"))
    batch = [
        NotificationRequest(
            recipient_id="user", channel=Channel.EMAIL, event_id="invoice", body=str(i)
        )
        for i in range(12)
    ]
    result = await service.send_batch(batch, concurrency=8)
    assert result[0] and result[1:] == [""] * 11
    task = await service.queue.claim(Channel.EMAIL)
    assert task.notification_id == result[0]
    assert task.request.body == "0"
    await service.queue.ack(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [0, -1, True, 1.5, None])
async def test_batch_rejects_invalid_concurrency_even_when_empty(concurrency):
    with pytest.raises(ValueError, match="concurrency"):
        await NotificationService().send_batch([], concurrency=concurrency)
