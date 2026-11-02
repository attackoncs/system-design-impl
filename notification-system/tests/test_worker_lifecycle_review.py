"""A reused Worker must publish readiness separately for each running loop."""

import asyncio

import pytest

from notification_system.config import RetryConfig
from notification_system.log import NotificationLog
from notification_system.models import Channel
from notification_system.provider import SimulatedProvider
from notification_system.queue import MessageQueue
from notification_system.retry import RetryHandler
from notification_system.tracker import EventTracker
from notification_system.worker import Worker


def make_worker():
    queue = MessageQueue()
    tracker = EventTracker()
    retry = RetryHandler(RetryConfig(), queue, tracker, NotificationLog())
    return Worker(
        "reused-worker",
        Channel.EMAIL,
        queue,
        SimulatedProvider(Channel.EMAIL, latency_ms=0),
        tracker,
        retry,
    )


async def finish_first_run(worker, task, exit_mode):
    if exit_mode == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await worker.stop()
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.parametrize("exit_mode", ["stop", "cancel"])
async def test_reused_worker_honors_immediate_stop_after_readiness(exit_mode):
    worker = make_worker()
    first = asyncio.create_task(worker.start())
    await worker.wait_started()
    await finish_first_run(worker, first, exit_mode)

    second = asyncio.create_task(worker.start())
    # Do not wrap this in wait_for: that would schedule another task and hide
    # the stale-readiness race by letting the second start run first.
    await worker.wait_started()
    await worker.stop()
    await asyncio.wait_for(second, timeout=1)
    assert not worker.is_running


@pytest.mark.parametrize("exit_mode", ["stop", "cancel"])
async def test_readiness_waits_for_a_new_run_after_worker_exit(exit_mode):
    worker = make_worker()
    task = asyncio.create_task(worker.start())
    await worker.wait_started()
    await finish_first_run(worker, task, exit_mode)

    waiter = asyncio.create_task(worker.wait_started())
    try:
        await asyncio.sleep(0)
        assert not waiter.done(), "previous run must not satisfy readiness"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
