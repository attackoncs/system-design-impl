"""New hints are retained while an earlier batch is awaiting acknowledgement."""
import asyncio
import pytest
from kv_store.cluster.hinted_handoff import HintedData, HintedHandoffManager


@pytest.mark.asyncio
async def test_new_hint_during_delivery_is_not_discarded():
    entered, release = asyncio.Event(), asyncio.Event()
    batches = []
    async def deliver(target, hints):
        batches.append(list(hints))
        entered.set()
        await release.wait()
        return True
    manager = HintedHandoffManager('source', deliver_func=deliver)
    first = HintedData('target', 'one', b'1', 1.0, 'source')
    second = HintedData('target', 'two', b'2', 2.0, 'source')
    manager.store_hint(first)
    task = asyncio.create_task(manager.deliver_hints('target'))
    await asyncio.wait_for(entered.wait(), 1)
    manager.store_hint(second)
    release.set()
    assert await asyncio.wait_for(task, 1)
    assert batches == [[first]]
    assert manager.get_pending_hints('target') == [second]
    assert await manager.deliver_hints('target')
    assert manager.pending_count == 0
