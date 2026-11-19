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
    group = asyncio.gather(*tasks)
    try:
        # Deliver cancellation once, then retain ownership of child cleanup.
        return await asyncio.shield(group)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            # A group can finish before every child's finally block has exited.
            await _wait_owned(asyncio.gather(*tasks, group, return_exceptions=True))
        except asyncio.CancelledError:
            pass
        raise
