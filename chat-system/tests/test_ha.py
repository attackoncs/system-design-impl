"""Real Sentinel integration; destructive actions restricted to an explicit disposable lab."""
import asyncio
import json
import os
from pathlib import Path
import time
import uuid

import pytest

pytest.importorskip("redis")
from chat_system.redis_backend import RedisBackend, WriteUncertainError
from chat_system.distributed import DistributedChatService


def test_unconfirmed_write_is_retryable_without_duplicate():
    url = os.environ.get("CHAT_TEST_REDIS_URL")
    if not url:
        pytest.skip("real Redis not configured")

    async def scenario():
        backend = RedisBackend.from_url(url, "test_wait_" + uuid.uuid4().hex)
        try:
            channel = await backend.create_channel("a", ["b"], "direct", 2)
            backend.wait_replicas = 100  # Intentionally impossible in the disposable test environment.
            backend.wait_timeout_ms = 25
            with pytest.raises(WriteUncertainError):
                await backend.append(channel.channel_id, "a", "uncertain", "same-key")
            original = (await backend.sync("b"))[0]
            backend.wait_replicas = 0
            retried, created, _ = await backend.append(channel.channel_id, "a", "uncertain", "same-key")
            assert not created and retried == original
            assert await backend.redis.xlen(backend.key("events")) == 1
            assert await backend.redis.xlen(backend.key("push")) == 1
        finally:
            keys = [k async for k in backend.redis.scan_iter(match=backend.prefix + "*")]
            if keys:
                await backend.redis.delete(*keys)
            await backend.close()
    asyncio.run(scenario())


def test_sentinel_promotes_replica_and_preserves_confirmed_messages():
    directory = os.environ.get("CHAT_TEST_REDIS_LAB")
    if not directory:
        pytest.skip("CHAT_TEST_REDIS_LAB not configured; failover not exercised")
    root = Path(directory)
    config = json.loads((root / "ready.json").read_text())

    async def scenario():
        backend = RedisBackend.from_sentinel([tuple(e) for e in config["sentinels"]],
                                             namespace="test_ha_" + uuid.uuid4().hex)
        services = []
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    token = await backend.issue("alice")
                    break
                except Exception:
                    if time.monotonic() > deadline:
                        raise
                    await asyncio.sleep(.2)
            a_node = await DistributedChatService(backend, "a", "ws://a", poll_interval=.1).start()
            b_node = await DistributedChatService(backend, "b", "ws://b", poll_interval=.1).start()
            services = [a_node, b_node]
            a = await a_node.connect(token, "phone")
            b = await b_node.connect(await backend.issue("bob"), "phone")
            channel = await a_node.create_direct(a, "bob")
            # Confirm both replicas before the deliberate primary crash.
            backend.wait_replicas = 2
            deadline = time.monotonic() + 15
            while True:
                try:
                    before = await a_node.send(a, channel.channel_id, "before crash", "stable")
                    break
                except WriteUncertainError:
                    if time.monotonic() > deadline:
                        raise
                    await asyncio.sleep(.1)
            assert (await asyncio.wait_for(b.queue.get(), 5))["message"]["message_id"] == before.message_id
            backend.wait_replicas = 1
            old = await backend._sentinel.discover_master("chat-primary")
            assert old[1] == config["primary"]
            started = time.monotonic()
            (root / "control.json").write_text(json.dumps({"kill": old[1]}))
            deadline = time.monotonic() + 30
            while True:
                try:
                    current = await backend._sentinel.discover_master("chat-primary")
                    if current != old:
                        after = await a_node.send(a, channel.channel_id, "after crash", "after")
                        break
                except Exception:
                    pass
                if time.monotonic() > deadline:
                    raise AssertionError("automatic failover did not recover within 30s")
                await asyncio.sleep(.1)
            assert await backend.authenticate(token) == "alice"
            assert await b_node.sync(b) == [before, after]
            deadline = time.monotonic() + 10
            while True:
                event = await asyncio.wait_for(b.queue.get(), max(.1, deadline - time.monotonic()))
                if event.get("message", {}).get("message_id") == after.message_id:
                    break
            retry, created, _ = await backend.append(channel.channel_id, "alice", "before crash", "stable")
            assert retry == before and not created
            assert await backend.redis.xlen(backend.key("events")) == 2
            assert await backend.redis.xlen(backend.key("push")) == 2
            report = {"old_primary": old[1], "new_primary": current[1],
                      "recovery_seconds": round(time.monotonic() - started, 3),
                      "confirmed_messages_preserved": True, "retry_duplicates": 0}
            # A retry that changes no message data must still require confirmation.
            from redis.asyncio import Redis
            replicas = [endpoint for endpoint in await backend._sentinel.discover_slaves("chat-primary")
                        if endpoint != old]  # Sentinel can briefly list the deliberately killed former primary.
            assert replicas
            clients = [Redis(host=host, port=port, decode_responses=True) for host, port in replicas]
            try:
                await asyncio.gather(*(client.execute_command("CLIENT", "PAUSE", 1500, "ALL") for client in clients))
                backend.wait_timeout_ms = 50
                with pytest.raises(WriteUncertainError):
                    await backend.append(channel.channel_id, "alice", "before crash", "stable")
                await asyncio.sleep(1.6)
                backend.wait_timeout_ms = 1000
                deadline = time.monotonic() + 10
                while True:
                    try:
                        retry, created, _ = await backend.append(channel.channel_id, "alice", "before crash", "stable")
                        break
                    except WriteUncertainError:
                        if time.monotonic() > deadline:
                            raise
                        await asyncio.sleep(.1)
                assert retry == before and not created
                assert await backend.redis.xlen(backend.key("events")) == 2
            finally:
                for client in clients:
                    await client.aclose()
            report["unconfirmed_retry_rejected"] = True
            (root / "result.json").write_text(json.dumps(report, indent=2))
        finally:
            for service in services:
                await service.close()
            keys = [k async for k in backend.redis.scan_iter(match=backend.prefix + "*")]
            if keys:
                await backend.redis.delete(*keys)
            await backend.close()
    asyncio.run(scenario())


def test_indexed_admission_movement_and_bounded_expiry():
    url = os.environ.get("CHAT_TEST_REDIS_URL")
    if not url:
        pytest.skip("real Redis not configured")
    async def scenario():
        backend = RedisBackend.from_url(url, "test_capacity_" + uuid.uuid4().hex)
        try:
            assert await backend.renew_node("a", "epoch-a", "ws://a", "local", 32, 30)
            assert await backend.renew_node("b", "epoch-b", "ws://b", "local", 32, 30)
            admissions = await asyncio.gather(*(backend.admit(str(i), "phone", str(i), "a", "epoch-a", 30, 32)
                                               for i in range(64)))
            assert sum(admissions) == 32
            admitted = str(admissions.index(True))
            assert await backend.admit(admitted, "phone", "moved", "b", "epoch-b", 30, 32)
            assert await backend.redis.zcard(backend.key("node_devices:a:epoch-a")) == 31
            assert await backend.admit("replacement", "phone", "new", "a", "epoch-a", 30, 32)
            assert not await backend.session_operation(admitted, "phone", admitted, "a", instance="epoch-a")
            assert (await backend.select()).server_id == "b"
            # A new incarnation does not inherit the old incarnation's capacity.
            await backend.remove_node("a", "epoch-a")
            assert await backend.renew_node("a", "epoch-next", "ws://a", "local", 32, 30)
            assert await backend.admit("expiring", "phone", "ttl", "a", "epoch-next", .05, 32)
            await asyncio.sleep(.06)
            assert await backend.prune_sessions(limit=1) == 1
            assert await backend.redis.hget(backend.key("sessions"), backend.device_key("expiring", "phone")) is None
            assert await backend.redis.zcard(backend.key("node_devices:a:epoch-next")) == 0
        finally:
            keys = [k async for k in backend.redis.scan_iter(match=backend.prefix + "*")]
            if keys:
                await backend.redis.delete(*keys)
            await backend.close()
    asyncio.run(scenario())
