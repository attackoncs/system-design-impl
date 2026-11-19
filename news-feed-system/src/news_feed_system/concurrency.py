"""Owned async work: drain blocking transactions and child tasks on cancel."""

import asyncio


async def complete(operation):
    """Keep ownership until local work finishes, even after repeated cancellation.

    The operation's own error wins over cancellation. Use this for bounded
    transactions and cleanup, not for normal provider or read work.
    """
    task = asyncio.ensure_future(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def run_sync(function, *args):
    return await complete(asyncio.to_thread(function, *args))


async def gather_owned(*coroutines):
    tasks = [asyncio.create_task(coro) for coro in coroutines]
    group = asyncio.gather(*tasks)
    try:
        # Do not let a second caller cancellation reach children already in
        # their finally blocks; cancellation is delivered explicitly once.
        return await asyncio.shield(group)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            # The first gather can finish on one failed child while others
            # still clean up. Observe every child before returning its error.
            await complete(asyncio.gather(*tasks, group, return_exceptions=True))
        except asyncio.CancelledError:
            pass
        raise
