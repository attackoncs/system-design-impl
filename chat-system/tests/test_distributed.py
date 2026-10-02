"""Real Redis tests. Set CHAT_TEST_REDIS_URL to an isolated test Redis endpoint.

Each test owns a random namespace; no flushdb or deletion outside that prefix.
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

pytest.importorskip("redis")
from chat_system import AuthenticationError, ChatConfig, ChatError
from chat_system.client import ReconnectingChatClient
from chat_system.cluster import DiscoveryHTTPServer
from chat_system.distributed import DistributedChatService
from chat_system.push import HTTPWebhookNotifier, PushWorker
from chat_system.redis_backend import RedisBackend


@pytest.fixture
def redis_url():
    url = os.environ.get("CHAT_TEST_REDIS_URL")
    if not url:
        pytest.skip("CHAT_TEST_REDIS_URL not configured; real Redis tests not run")
    return url


@asynccontextmanager
async def backend_for(url):
    backend = RedisBackend.from_url(url, "test_chat_" + uuid.uuid4().hex)
    await backend.redis.ping()
    try:
        yield backend
    finally:
        keys = [key async for key in backend.redis.scan_iter(match=backend.prefix + "*")]
        assert all(key.startswith(backend.prefix) for key in keys)
        if keys:
            await backend.redis.delete(*keys)
        await backend.close()


async def node(backend, name, **kwargs):
    return await DistributedChatService(backend, name, "ws://" + name, node_lease=3,
                                        poll_interval=0.05, **kwargs).start()


async def login(service, user, device="phone"):
    return await service.connect(await service.auth.issue(user), device)


async def eventual(predicate, timeout=5):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        result = await predicate()
        if result:
            return result
        await asyncio.sleep(0.02)
    raise AssertionError("condition did not become true")


def test_cross_node_direct_group_sync_and_idempotency(redis_url):
    async def scenario():
        async with backend_for(redis_url) as backend:
            a_node, b_node = await node(backend, "a"), await node(backend, "b")
            try:
                a = await login(a_node, "alice")
                b = await login(b_node, "bob")
                laptop = await login(a_node, "bob", "laptop")
                channel = await a_node.create_direct(a, "bob")
                assert (await b_node.create_direct(b, "alice")).channel_id == channel.channel_id
                sent = await a_node.send(a, channel.channel_id, "跨节点", "request-1")
                event = await asyncio.wait_for(b.queue.get(), 3)
                assert event["message"] == asdict(sent)
                assert (await asyncio.wait_for(laptop.queue.get(), 3))["message"] == asdict(sent)
                assert await b_node.sync(b) == [sent]
                assert await a_node.send(a, channel.channel_id, "跨节点", "request-1") == sent
                with pytest.raises(ChatError):
                    await a_node.send(a, channel.channel_id, "conflict", "request-1")
                group = await a_node.create_group(a, ["bob", "carol"])
                group_message = await b_node.send(b, group.channel_id, "group", "bob-1")
                assert (await b_node.history(b, group.channel_id)) == [group_message]
                assert await backend.redis.xlen(backend.key("events")) == 2
            finally:
                await a_node.close()
                await b_node.close()

    asyncio.run(scenario())


def test_shared_presence_device_fencing_and_atomic_write_guard(redis_url):
    async def scenario():
        async with backend_for(redis_url) as backend:
            a_node, b_node = await node(backend, "a"), await node(backend, "b")
            try:
                token = await backend.issue("user")
                old = await a_node.connect(token, "phone")
                observer = await login(a_node, "observer")
                assert await a_node.subscribe_presence(observer, ["user"]) == {"user": True}
                channel = await a_node.create_direct(old, "observer")
                replacement = await b_node.connect(token, "phone")
                with pytest.raises(AuthenticationError):
                    await a_node.heartbeat(old)
                with pytest.raises(ChatError):
                    await backend.append(channel.channel_id, "user", "late stale send", "old",
                                          session=old, node="a", instance=a_node.instance)
                await a_node.disconnect(old)
                assert await b_node.is_online("user")
                await b_node.heartbeat(replacement)
                await b_node.disconnect(replacement)
                async def offline():
                    return not await backend.is_online("user")
                await eventual(offline)
                event = await asyncio.wait_for(observer.queue.get(), 3)
                assert event == {"type": "presence", "user_id": "user", "online": False}
            finally:
                await a_node.close()
                await b_node.close()

    asyncio.run(scenario())


def test_capacity_discovery_and_duplicate_node_fencing(redis_url):
    async def scenario():
        async with backend_for(redis_url) as backend:
            a_node = await node(backend, "a", region="east", capacity=1)
            b_node = await node(backend, "b", region="west", capacity=2)
            try:
                assert (await backend.select("east")).server_id == "a"
                token = await backend.issue("a")
                first = await a_node.connect(token, "phone")
                replacement = await a_node.connect(token, "phone")
                assert first.closed
                assert (await backend.select("east")).server_id == "b"
                with pytest.raises(ChatError):
                    await a_node.connect(await backend.issue("another"), "phone")
                duplicate = DistributedChatService(backend, "a", "ws://duplicate")
                with pytest.raises(ChatError):
                    await duplicate.start()
                assert not await backend.checkpoint("a", "wrong-instance", "9999999999999-0")
                assert (await backend.select()).server_id == "b"
                await a_node.disconnect(replacement)
                assert (await backend.select("east")).server_id == "a"
            finally:
                await a_node.close()
                await b_node.close()

    asyncio.run(scenario())


def test_concurrent_cross_node_retry_and_unique_order(redis_url):
    async def scenario():
        async with backend_for(redis_url) as backend:
            a_node, b_node = await node(backend, "a"), await node(backend, "b")
            try:
                token = await backend.issue("sender")
                a, b = await a_node.connect(token, "phone"), await b_node.connect(token, "laptop")
                channel = await a_node.create_direct(a, "recipient")
                messages = await asyncio.gather(
                    *(service.send(session, channel.channel_id, "same", "same-key")
                      for service, session in [(a_node, a), (b_node, b)] * 5))
                assert len({m.message_id for m in messages}) == 1
                unique = await asyncio.gather(
                    *(a_node.send(a, channel.channel_id, str(i), str(i)) for i in range(20)))
                assert len({m.message_id for m in unique}) == 20
                inbox = await backend.sync("recipient")
                assert len(inbox) == 21
                assert [m.message_id for m in inbox] == sorted(m.message_id for m in inbox)
                assert await backend.redis.xlen(backend.key("push")) == 21
            finally:
                await a_node.close()
                await b_node.close()

    asyncio.run(scenario())


def test_durable_push_retry_worker_replacement_and_dead_letter(redis_url):
    class Broken:
        async def deliver(self, user, message, key):
            raise RuntimeError("provider down")

    async def scenario():
        async with backend_for(redis_url) as backend:
            service = await node(backend, "a")
            try:
                a = await login(service, "a")
                channel = await service.create_direct(a, "offline")
                await service.send(a, channel.channel_id, "retry", "1")
                worker = PushWorker(backend, Broken(), reclaim_idle_ms=1, retry_base=0, max_attempts=2)
                assert await worker.run_once() == 1
                assert await backend.redis.hlen(backend.key("push_retries")) == 1
                await asyncio.sleep(0.01)
                recovered = PushWorker(backend, reclaim_idle_ms=1, retry_base=0)
                await recovered.run_once()
                assert await backend.redis.hlen(backend.key("push_deliveries")) == 1
                assert (await backend.redis.xpending(backend.key("push"), worker.group))["pending"] == 0
                await service.send(a, channel.channel_id, "dead letter", "2")
                await worker.run_once()
                await asyncio.sleep(0.01)
                await worker.run_once()
                assert await backend.redis.xlen(backend.key("push_dead_letters")) == 1
            finally:
                await service.close()

    asyncio.run(scenario())


def test_http_push_adapter_and_online_recipient_filter(redis_url):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.headers["Idempotency-Key"],
                             json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    async def scenario():
        async with backend_for(redis_url) as backend:
            service = await node(backend, "a")
            try:
                a = await login(service, "a")
                await login(service, "online")
                group = await service.create_group(a, ["online", "offline"])
                await service.send(a, group.channel_id, "hello", "1")
                worker = PushWorker(backend, HTTPWebhookNotifier(f"http://127.0.0.1:{http.server_port}/push"))
                await worker.run_once()
                assert len(received) == 1
                assert received[0][1]["user_id"] == "offline"
                assert received[0][1]["message"]["content"] == "hello"
                assert received[0][0].endswith(":offline:1")
            finally:
                await service.close()
    try:
        asyncio.run(scenario())
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_independent_process_node_crash_and_client_reconnect(redis_url, tmp_path):
    from websockets.asyncio.client import connect
    from test_transport import request

    async def scenario():
        async with backend_for(redis_url) as backend:
            namespace = backend.prefix[1:backend.prefix.index("}")]
            a_port, b_port = free_port(), free_port()
            processes, logs = [], []
            api = await DiscoveryHTTPServer(backend).start(port=0)
            discovery_url = f"http://127.0.0.1:{api.sockets[0].getsockname()[1]}/discover"
            client = None
            try:
                for name, port, region in [("a", a_port, "east"), ("b", b_port, "west")]:
                    log = (tmp_path / (name + ".log")).open("w+", encoding="utf-8")
                    logs.append(log)
                    cmd = [sys.executable, "-m", "chat_system.cluster", "--redis-url", redis_url,
                           "--namespace", namespace, "node", "--node-id", name, "--port", str(port),
                           "--public-url", f"ws://127.0.0.1:{port}", "--region", region,
                           "--lease", "0.8", "--poll", "0.1"]
                    processes.append(subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
                async def registered():
                    return await backend.redis.hlen(backend.key("nodes")) == 2
                await eventual(registered, timeout=10)
                alice_token, bob_token = await backend.issue("alice"), await backend.issue("bob")
                client = ReconnectingChatClient(discovery_url, bob_token, "phone", region="west",
                                                  cursor_file=tmp_path / "cursor", retry_delay=0.05)
                assert await client.receive() == []
                assert client.endpoint.endswith(str(b_port))
                async with connect(f"ws://127.0.0.1:{a_port}") as a:
                    await request(a, 1, "login", token=alice_token, device_id="phone")
                    direct = await request(a, 2, "direct", recipient_id="bob")
                    channel = direct["result"]["channel_id"]
                    sent = await request(a, 3, "send", channel_id=channel, content="before failure",
                                         client_message_id="1")
                    assert sent["ok"]
                    page = await client.receive()
                    assert [m["content"] for m in page] == ["before failure"]
                    client.acknowledge(page)
                    assert await backend.is_online("bob")
                    initial_group = await request(a, 30, "group", members=["bob", "carol"])
                    await request(a, 31, "send", channel_id=initial_group["result"]["channel_id"],
                                  content="cross-node group", client_message_id="group-first")
                    page = await client.receive()
                    assert [m["content"] for m in page] == ["cross-node group"]
                    client.acknowledge(page)
                    # Kill only the process created by this test, simulating an abrupt node failure.
                    processes[1].kill()
                    await asyncio.to_thread(processes[1].wait, 5)
                    async def lease_expired():
                        return (await backend.select("west")).server_id == "a"
                    await eventual(lease_expired)
                    assert not await backend.is_online("bob")
                    await request(a, 4, "send", channel_id=channel, content="after failure", client_message_id="2")
                    page = await client.receive()
                    assert client.endpoint.endswith(str(a_port))
                    assert [m["content"] for m in page] == ["after failure"]
                    assert await backend.is_online("bob")
                    client.acknowledge(page)
                    assert int((tmp_path / "cursor").read_text()) == page[-1]["message_id"]
                    group = await request(a, 5, "group", members=["bob", "carol"])
                    await request(a, 6, "send", channel_id=group["result"]["channel_id"],
                                  content="group", client_message_id="3")
                    assert [m["content"] for m in await client.receive()] == ["group"]
            finally:
                if client:
                    await client.close()
                api.close()
                await api.wait_closed()
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                        await asyncio.to_thread(process.wait, 5)
                for log in logs:
                    log.close()

    asyncio.run(scenario())


def test_group_join_history_and_cross_node_revocation(redis_url):
    async def scenario():
        async with backend_for(redis_url) as backend:
            a_node, b_node = await node(backend, "a"), await node(backend, "b")
            try:
                a, c = await login(a_node, "a"), await login(b_node, "c")
                group = await a_node.create_group(a, ["b"])
                await a_node.send(a, group.channel_id, "private-before-join", "1")
                await a_node.add_member(a, group.channel_id, "c")
                new = await a_node.send(a, group.channel_id, "after-join", "2")
                assert await b_node.history(c, group.channel_id) == [new]
                assert await b_node.sync(c) == [new]
                await backend.revoke(c.token)
                with pytest.raises(AuthenticationError):
                    await b_node.sync(c)
            finally:
                await a_node.close()
                await b_node.close()

    asyncio.run(scenario())


def test_node_restart_replays_uncheckpointed_events(redis_url):
    async def scenario():
        async with backend_for(redis_url) as backend:
            service = await node(backend, "a")
            sender = await login(service, "a")
            channel = await service.create_direct(sender, "b")
            sent = await service.send(sender, channel.channel_id, "durable", "1")
            await service.close()
            # Simulate a crash before checkpointing the committed broker event.
            await backend.redis.hset(backend.key("node_cursors"), "a", "0-0")
            replacement = await node(backend, "a")
            try:
                recipient = await login(replacement, "b")
                assert await replacement.sync(recipient) == [sent]
                async def checkpointed():
                    value = await backend.redis.hget(backend.key("node_cursors"), "a")
                    return value != "0-0"
                await eventual(checkpointed)
                assert replacement.background_error is None
            finally:
                await replacement.close()

    asyncio.run(scenario())


def test_broker_network_failure_cannot_acknowledge_volatile_write(redis_url):
    from redis.asyncio import Redis
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError
    from redis.retry import Retry
    from redis.backoff import NoBackoff

    async def scenario():
        async with backend_for(redis_url) as backend:
            service = await node(backend, "a")
            disconnected = Redis(host="127.0.0.1", port=free_port(), decode_responses=True,
                                 socket_connect_timeout=0.1, retry=Retry(NoBackoff(), 0))
            original = backend.redis
            try:
                a = await login(service, "a")
                channel = await service.create_direct(a, "b")
                backend.redis = disconnected
                with pytest.raises((RedisConnectionError, RedisTimeoutError)):
                    await service.send(a, channel.channel_id, "must not be acknowledged", "1")
                backend.redis = original
                assert await backend.sync("b") == []
            finally:
                backend.redis = original
                await disconnected.aclose()
                await service.close()

    asyncio.run(scenario())
