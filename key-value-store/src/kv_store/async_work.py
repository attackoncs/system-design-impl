"""Finish owned commits before exposing cancellation to their caller."""

from __future__ import annotations

import asyncio
from functools import wraps


async def complete(operation):
    """Drain a shielded operation even after repeated cancellation.

    Cancelling asyncio.to_thread does not stop its thread. Letting a lock go
    before that thread finishes can race a later flush/truncate. Actual commit
    errors take precedence; otherwise cancellation is re-raised after completion.
    Use only around bounded local commits, never a whole remote repair round.
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


def finish_on_cancel(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        return await complete(function(*args, **kwargs))

    return wrapped
