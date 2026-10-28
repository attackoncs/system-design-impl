"""Bound synchronous work and retain ownership until accepted I/O has exited."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from .core import Error


async def finish(awaitable):
    """Drain owned work even when its awaiting task is cancelled.

    Cancellation cannot stop a running SQLite/Redis call. Shielding alone would
    orphan it; this helper also waits for completion before propagating cancel.
    """
    future = asyncio.ensure_future(awaitable)
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    try:
        result = future.result()
    except BaseException:
        if cancelled:
            raise asyncio.CancelledError() from None
        raise
    if cancelled:
        raise asyncio.CancelledError()
    return result


class WorkPool:
    """At most workers calls submitted; waiters occupy admitted sessions only."""

    def __init__(self, workers=16):
        if type(workers) is not int or not 1 <= workers <= 128:
            raise ValueError("workers must be an integer in 1..128")
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="nearby-io"
        )
        self._slots = asyncio.Semaphore(workers)
        self._pending = set()
        self._closing = False

    async def call(self, function, *args):
        if self._closing:
            raise Error(503, "service is draining")
        await self._slots.acquire()
        if self._closing:
            self._slots.release()
            raise Error(503, "service is draining")
        try:
            future = asyncio.get_running_loop().run_in_executor(
                self._executor, function, *args
            )
        except BaseException:
            self._slots.release()
            raise
        self._pending.add(future)

        def completed(done):
            self._pending.discard(done)
            self._slots.release()

        future.add_done_callback(completed)
        return await finish(future)

    async def close(self):
        self._closing = True
        pending = asyncio.gather(*tuple(self._pending), return_exceptions=True)
        try:
            await finish(pending)
        finally:
            # All submitted futures have exited, so this does not block on I/O.
            self._executor.shutdown(wait=True)
