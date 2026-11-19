"""Redis history and sync must stop reading when a network page is full."""

import asyncio
import os

import pytest
from chat_system.models import ChatError
from test_distributed import backend_for, login, node


@pytest.fixture
def redis_url():
    url = os.environ.get("CHAT_TEST_REDIS_URL")
    if not url:
        pytest.skip("CHAT_TEST_REDIS_URL not configured; real Redis tests not run")
    return url


def test_bounded_redis_pages_keep_the_last_returned_cursor(redis_url):
    async def scenario():
        async with backend_for(redis_url) as backend:
            group = await backend.create_channel("a", ["b"], "group", 100)
            before, *_ = await backend.append(group.channel_id, "a", "old", "old")
            await backend.add_member(group.channel_id, "a", "new", 100)
            body = "😀" * 120
            first, *_ = await backend.append(group.channel_id, "a", body, "first")
            second, *_ = await backend.append(group.channel_id, "a", body, "second")

            assert await backend.sync_bounded("new", byte_limit=1000) == [first]
            assert await backend.sync_bounded(
                "new", after=first.message_id, byte_limit=1000
            ) == [second]
            assert await backend.history_bounded(
                group.channel_id, "new", byte_limit=1000
            ) == [first]
            assert await backend.history_bounded(
                group.channel_id, "new", after=first.message_id, byte_limit=1000
            ) == [second]
            assert await backend.sync("new") == [first, second]
            assert await backend.history(group.channel_id, "b") == [
                before,
                first,
                second,
            ]
            with pytest.raises(ChatError, match="not a channel member"):
                await backend.history_bounded(group.channel_id, "outsider")

    asyncio.run(scenario())


def test_bounded_redis_page_rejects_an_oversized_first_message(redis_url):
    async def scenario():
        async with backend_for(redis_url) as backend:
            channel = await backend.create_channel("a", ["b"], "direct", 2)
            await backend.append(channel.channel_id, "a", "hello", "first")
            with pytest.raises(ChatError, match="message exceeds page byte limit"):
                await backend.sync_bounded("b", byte_limit=10)
            with pytest.raises(ChatError, match="message exceeds page byte limit"):
                await backend.history_bounded(channel.channel_id, "b", byte_limit=10)

    asyncio.run(scenario())


def test_distributed_network_sync_uses_bounded_store_page(redis_url):
    async def scenario():
        async with backend_for(redis_url) as backend:
            service = await node(backend, "budget-node")
            try:
                sender = await login(service, "a")
                recipient = await login(service, "b")
                channel = await service.create_direct(sender, "b")
                body = "😀" * 99999
                sent = [
                    await service.send(sender, channel.channel_id, body, str(index))
                    for index in range(16)
                ]
                original_hmget = backend.redis.hmget
                fetched_counts = []

                async def measured_hmget(key, ids):
                    rows = await original_hmget(key, ids)
                    fetched_counts.append(len(rows))
                    return rows

                backend.redis.hmget = measured_hmget
                assert await service.async_sync(recipient) == sent[:4]
                assert (
                    await service.async_sync(recipient, after=sent[3].message_id)
                    == sent[4:8]
                )
                assert (
                    await service.async_history(recipient, channel.channel_id)
                    == sent[:4]
                )
                assert fetched_counts and max(fetched_counts) <= 8
            finally:
                await service.close()

    asyncio.run(scenario())
