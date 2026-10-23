"""Worker pool lifecycle prevents orphan workers on repeated starts."""
import pytest
from notification_system.models import Channel
from notification_system.queue import MessageQueue
from notification_system.worker import WorkerPool


@pytest.mark.asyncio
async def test_duplicate_start_is_rejected():
    pool = WorkerPool(MessageQueue(), {Channel.EMAIL: object()}, None, None, workers_per_channel=1)
    await pool.start()
    try:
        with pytest.raises(RuntimeError, match='already running'):
            await pool.start()
    finally:
        await pool.stop(graceful=False)
    assert not pool.is_running
