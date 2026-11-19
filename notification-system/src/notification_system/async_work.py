"""Drain owned local work before exposing cancellation to its caller."""

from __future__ import annotations

import asyncio


async def complete(operation):
    """Finish an accepted commit or cleanup despite repeated cancellation.

    Cancelling a to_thread wrapper does not stop the thread. Shield its task
    on every wait, so the caller keeps its lock and ownership until the work
    finishes. Real operation failures take precedence over caller cancellation.
    Use for bounded local acceptance/cleanup, never normal provider delivery.
    """
    task = asyncio.ensure_future(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result
