"""Notification groups retain child cleanup after cancellation or failure."""

import asyncio

import pytest
from chat_system.concurrency import gather_owned


@pytest.mark.parametrize("failure", [False, True])
def test_owned_group_drains_cleanup_and_preserves_original_result(failure):
    async def scenario():
        started, cleaning, release, finished = (asyncio.Event() for _ in range(4))

        async def slow():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                finished.set()

        async def other():
            await started.wait()
            if failure:
                raise OSError("notification failed")
            await asyncio.Event().wait()

        pending = asyncio.create_task(gather_owned(slow(), other()))
        try:
            await asyncio.wait_for(started.wait(), 2)
            if not failure:
                pending.cancel()
            await asyncio.wait_for(cleaning.wait(), 2)
            pending.cancel()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending), 0.02)
        finally:
            release.set()
            result = (await asyncio.gather(pending, return_exceptions=True))[0]
        assert finished.is_set()
        assert isinstance(result, OSError if failure else asyncio.CancelledError)

    asyncio.run(scenario())
