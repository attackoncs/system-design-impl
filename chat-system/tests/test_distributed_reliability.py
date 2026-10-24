"""Lifecycle races and real Redis fencing regressions for chapter 13."""

import asyncio
import contextlib
import http.server
import os
import threading
from types import SimpleNamespace
import urllib.error

import pytest

from chat_system.distributed import DistributedChatService
from chat_system.models import ChatError
from chat_system.push import HTTPWebhookNotifier, PushWorker
from chat_system.redis_backend import RedisBackend


class AdmissionBackend:
    def __init__(self):
        self.redis = SimpleNamespace(
            ping=self.ping,
            hget=self.hget,
            xrevrange=self.last,
        )
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.renewals = 0
        self.removed = 0
        self.remote = {}

    def key(self, name):
        return name

    async def ping(self):
        await asyncio.sleep(0)

    async def hget(self, *_):
        return None

    async def last(self, *_, **__):
        return []

    async def authenticate(self, _):
        return "alice"

    async def renew_node(self, *_):
        self.renewals += 1
        return True

    async def remove_node(self, *_):
        self.removed += 1

    async def admit(self, user, device, session_id, *_):
        self.entered.set()
        await self.release.wait()
        self.remote[(user, device)] = session_id
        return True

    async def session_operation(
        self, user, device, session_id, _, operation="check", **__
    ):
        if self.remote.get((user, device)) != session_id:
            return False
        if operation == "close":
            self.remote.pop((user, device))
        return True


async def idle():
    await asyncio.Event().wait()


async def lifecycle_service(backend):
    service = DistributedChatService(backend, "node", "ws://node")
    service._consume = idle
    service._maintenance = idle
    return await service.start()


def test_concurrent_start_owns_one_pair_of_background_tasks():
    async def scenario():
        backend = AdmissionBackend()
        service = await lifecycle_service(backend)
        tasks = tuple(service._tasks)
        try:
            await asyncio.gather(*(service.start() for _ in range(8)))
            assert backend.renewals == 1
            assert tuple(service._tasks) == tasks
        finally:
            await service.close()
        assert all(task.done() for task in tasks)

    asyncio.run(scenario())


def test_twice_cancelled_start_releases_a_committed_node_lease():
    async def scenario():
        backend = AdmissionBackend()
        entered, release = asyncio.Event(), asyncio.Event()
        original = backend.renew_node

        async def delayed_renew(*args):
            result = await original(*args)
            entered.set()
            await release.wait()
            return result

        backend.renew_node = delayed_renew
        service = DistributedChatService(backend, "node", "ws://node")
        service._consume = idle
        service._maintenance = idle
        startup = asyncio.create_task(service.start())
        await entered.wait()
        for _ in range(2):
            startup.cancel()
            await asyncio.sleep(0)
            assert not startup.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await startup
        assert not service.started
        assert service._tasks == []
        assert backend.removed == 1

    asyncio.run(scenario())


def test_close_waits_for_admission_and_removes_committed_remote_session():
    async def scenario():
        backend = AdmissionBackend()
        service = await lifecycle_service(backend)
        connect = asyncio.create_task(service.connect("token", "phone"))
        await backend.entered.wait()
        close = asyncio.create_task(service.close())
        while service.started:
            await asyncio.sleep(0)
        assert not close.done()
        backend.release.set()
        with pytest.raises(ChatError, match="closed during admission"):
            await connect
        await close
        assert backend.remote == {}
        assert service._sessions == {}
        assert backend.removed == 1

    asyncio.run(scenario())


def test_cancelled_admission_drains_and_closes_the_new_session():
    async def scenario():
        backend = AdmissionBackend()
        service = await lifecycle_service(backend)
        try:
            connect = asyncio.create_task(service.connect("token", "phone"))
            await backend.entered.wait()
            connect.cancel()
            await asyncio.sleep(0)
            assert not connect.done()
            connect.cancel()
            await asyncio.sleep(0)
            assert not connect.done()
            backend.release.set()
            with pytest.raises(asyncio.CancelledError):
                await connect
            assert backend.remote == {}
            assert service._sessions == {}
        finally:
            await service.close()

    asyncio.run(scenario())


def test_cancelled_close_still_drains_admission_and_background_tasks():
    async def scenario():
        backend = AdmissionBackend()
        service = await lifecycle_service(backend)
        tasks = tuple(service._tasks)
        connect = asyncio.create_task(service.connect("token", "phone"))
        await backend.entered.wait()
        close = asyncio.create_task(service.close())
        while service.started:
            await asyncio.sleep(0)
        close.cancel()
        await asyncio.sleep(0)
        assert not close.done()
        close.cancel()
        await asyncio.sleep(0)
        assert not close.done()
        backend.release.set()
        with pytest.raises(ChatError):
            await connect
        with pytest.raises(asyncio.CancelledError):
            await close
        assert all(task.done() for task in tasks)
        assert backend.remote == {}
        assert service._sessions == {}
        assert backend.removed == 1

    asyncio.run(scenario())


def test_lease_loss_marks_node_unready_and_closes_local_sessions():
    async def scenario():
        backend = AdmissionBackend()
        backend.release.set()
        service = await lifecycle_service(backend)
        session = await service.connect("token", "phone")

        async def lost(*_):
            return False

        backend.renew_node = lost
        try:
            await DistributedChatService._maintenance(service)
            assert not service.started
            assert session.closed
            with pytest.raises(ChatError, match="not ready"):
                await service.connect("token", "other")
        finally:
            await service.close()

    asyncio.run(scenario())


def test_cancelled_webhook_waits_for_blocking_post_to_finish(monkeypatch):
    entered = threading.Event()
    finish = threading.Event()

    class Opener:
        def open(self, *_args, **_kwargs):
            entered.set()
            assert finish.wait(5)
            return contextlib.nullcontext(SimpleNamespace(status=200))

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())

    async def scenario():
        from chat_system.models import Message

        notifier = HTTPWebhookNotifier("http://localhost/push")
        task = asyncio.create_task(
            notifier.deliver(
                "b",
                Message(1, "channel", "a", "hello", 0, "client"),
                "identity",
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_webhook_redirects_are_rejected_without_forwarding_credentials():
    forwarded = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", "/accepted")
            self.end_headers()

        def do_GET(self):
            forwarded.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def scenario():
        from chat_system.models import Message

        notifier = HTTPWebhookNotifier(
            f"http://127.0.0.1:{server.server_port}/push",
            bearer_token="secret",
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            await notifier.deliver(
                "b", Message(1, "channel", "a", "hello", 0, "client"), "identity"
            )
        assert error.value.code == 302
        assert forwarded == []

    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"capacity": True},
        {"capacity": 1.5},
        {"node_lease": float("inf")},
        {"node_lease": True},
        {"poll_interval": float("nan")},
    ],
)
def test_distributed_limits_are_finite_and_typed(kwargs):
    with pytest.raises(ValueError):
        DistributedChatService(None, "node", "ws://node", **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reclaim_idle_ms": True},
        {"reclaim_idle_ms": 1.5},
        {"retry_base": float("nan")},
        {"max_attempts": True},
        {"delivery_timeout": float("inf")},
    ],
)
def test_push_limits_are_finite_and_typed(kwargs):
    with pytest.raises(ValueError):
        PushWorker(None, **kwargs)


@pytest.mark.parametrize(
    "url", ["http://", "ftp://example.test", "http://user:secret@example.test"]
)
def test_webhook_requires_a_host_and_no_embedded_credentials(url):
    with pytest.raises(ValueError):
        HTTPWebhookNotifier(url)


def test_replication_timeout_requires_integer_milliseconds():
    for value in (True, 1.5, float("nan")):
        with pytest.raises(ValueError):
            RedisBackend(None, wait_timeout_ms=value)


@pytest.fixture
def isolated_redis_url():
    url = os.environ.get("CHAT_TEST_REDIS_URL")
    if not url:
        pytest.skip("CHAT_TEST_REDIS_URL not configured; real Redis not exercised")
    pytest.importorskip("redis")
    return url


def test_real_redis_stale_push_lock_cannot_ack_retry_or_dead_letter(isolated_redis_url):
    from test_distributed import backend_for

    async def scenario():
        async with backend_for(isolated_redis_url) as backend:
            channel = await backend.create_channel("a", ["b"], "direct", 2)
            await backend.append(channel.channel_id, "a", "hello", "1")
            worker = PushWorker(backend)
            await worker.initialize()
            batch = await backend.redis.xreadgroup(
                worker.group, "owner", {backend.key("push"): ">"}
            )
            entry_id, fields = batch[0][1][0]
            lock = backend.key("push_lock:" + entry_id)
            await backend.redis.set(lock, "new-owner", px=10000)
            await backend.redis.hset(backend.key("push_retries"), entry_id, "preserved")
            assert not await worker._complete(entry_id, "old-owner", fields, "Failed")
            assert (await backend.redis.xpending(backend.key("push"), worker.group))[
                "pending"
            ] == 1
            assert (
                await backend.redis.hget(backend.key("push_retries"), entry_id)
                == "preserved"
            )
            assert await backend.redis.xlen(backend.key("push_dead_letters")) == 0
            assert await worker._complete(entry_id, "new-owner")
            assert (await backend.redis.xpending(backend.key("push"), worker.group))[
                "pending"
            ] == 0

    asyncio.run(scenario())


def test_real_redis_replayed_acknowledged_push_is_not_delivered_again(
    isolated_redis_url,
):
    from test_distributed import backend_for

    class CountingNotifier:
        def __init__(self):
            self.calls = 0

        async def deliver(self, *_):
            self.calls += 1

    async def scenario():
        async with backend_for(isolated_redis_url) as backend:
            channel = await backend.create_channel("a", ["b"], "direct", 2)
            await backend.append(channel.channel_id, "a", "hello", "1")
            notifier = CountingNotifier()
            worker = PushWorker(backend, notifier)
            await worker.initialize()
            batch = await backend.redis.xreadgroup(
                worker.group, "owner", {backend.key("push"): ">"}
            )
            entry_id, fields = batch[0][1][0]
            await worker._handle(entry_id, fields)
            await worker._handle(entry_id, fields)
            assert notifier.calls == 1

    asyncio.run(scenario())


def test_real_redis_push_lock_renews_while_other_consumer_reclaims(isolated_redis_url):
    from test_distributed import backend_for

    class SlowNotifier:
        def __init__(self):
            self.calls = 0
            self.started = asyncio.Event()
            self.finish = asyncio.Event()

        async def deliver(self, *_):
            self.calls += 1
            self.started.set()
            await self.finish.wait()

    async def scenario():
        async with backend_for(isolated_redis_url) as backend:
            channel = await backend.create_channel("a", ["b"], "direct", 2)
            await backend.append(channel.channel_id, "a", "slow", "1")
            notifier = SlowNotifier()
            owner = PushWorker(backend, notifier, reclaim_idle_ms=1)
            owner.lock_ttl_ms = 120
            other = PushWorker(backend, notifier, reclaim_idle_ms=1)
            delivery = asyncio.create_task(owner.run_once())
            await asyncio.wait_for(notifier.started.wait(), 2)
            try:
                await asyncio.sleep(0.25)
                await other.run_once()
                assert notifier.calls == 1
            finally:
                notifier.finish.set()
                await delivery
            assert (await backend.redis.xpending(backend.key("push"), owner.group))[
                "pending"
            ] == 0

    asyncio.run(scenario())


def test_real_redis_sequence_boundary_preserves_exact_order(isolated_redis_url):
    from test_distributed import backend_for

    async def scenario():
        async with backend_for(isolated_redis_url) as backend:
            channel = await backend.create_channel("a", ["b"], "direct", 2)
            await backend.redis.set(backend.key("sequence"), 9007199254740988)
            first, *_ = await backend.append(channel.channel_id, "a", "one", "1")
            second, *_ = await backend.append(channel.channel_id, "a", "two", "2")
            assert [first.message_id, second.message_id] == [
                9007199254740989,
                9007199254740990,
            ]
            assert await backend.sync("b", first.message_id) == [second]
            assert await backend.history(channel.channel_id, "b", first.message_id) == [
                second
            ]
            with pytest.raises(ChatError):
                await backend.append(channel.channel_id, "a", "over limit", "3")

    asyncio.run(scenario())


def test_real_redis_random_access_checks_original_recipient(isolated_redis_url):
    from test_distributed import backend_for

    async def scenario():
        async with backend_for(isolated_redis_url) as backend:
            group = await backend.create_channel("a", ["b"], "group", 100)
            before, *_ = await backend.append(group.channel_id, "a", "before", "1")
            await backend.add_member(group.channel_id, "a", "new", 100)
            after, *_ = await backend.append(group.channel_id, "a", "after", "2")
            assert await backend.get_visible_message(before.message_id, "b") == before
            assert await backend.get_visible_message(after.message_id, "new") == after
            for user in ("new", "outsider"):
                with pytest.raises(ChatError, match="not found"):
                    await backend.get_visible_message(before.message_id, user)
            for value in (True, 0, -1, "1", 1.5):
                with pytest.raises(ChatError, match="identifier"):
                    await backend.get_visible_message(value, "a")

    asyncio.run(scenario())
