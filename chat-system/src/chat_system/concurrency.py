"""Own blocking store work and asynchronous child tasks through cancellation."""

import asyncio


async def _wait_owned(task):
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
        except Exception:
            break
    if interrupted:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError()
    return task.result()


async def run_blocking(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    # Repeated caller cancellation must not abandon a database transaction.
    return await _wait_owned(task)


async def gather_owned(*coroutines):
    tasks = [asyncio.create_task(coro) for coro in coroutines]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await _wait_owned(asyncio.gather(*tasks, return_exceptions=True))
