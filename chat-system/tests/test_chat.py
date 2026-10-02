import asyncio

import pytest

from chat_system import (
    AuthenticationError, ChatConfig, ChatError, ChatService,
    Server, ServiceDiscovery, SQLiteMessageStore,
)


@pytest.fixture
def chat():
    service = ChatService()
    yield service
    service.store.close()


def login(chat, user, device="phone"):
    return chat.connect(chat.auth.issue(user), device)


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.mark.parametrize("token", ["wrong", "中文", None, 123, "x" * 257])
def test_invalid_credentials(chat, token):
    with pytest.raises(AuthenticationError):
        chat.connect(token, "phone")


def test_same_device_replacement_and_revocation(chat):
    token = chat.auth.issue("a")
    old = chat.connect(token, "phone")
    current = chat.connect(token, "phone")
    with pytest.raises(AuthenticationError):
        chat.sync(old)
    chat.auth.revoke(token)
    with pytest.raises(AuthenticationError):
        chat.sync(current)
    assert current.closed


def test_direct_pair_is_reused(chat):
    a, b = login(chat, "a"), login(chat, "b")
    direct = chat.create_direct(a, "b")
    assert chat.create_direct(b, "a") == direct
    with pytest.raises(ChatError):
        chat.create_direct(a, "a")


def test_group_authorization_limits_and_duplicates(chat):
    owner, member, stranger = (login(chat, u) for u in ("a", "b", "c"))
    group = chat.create_group(owner, ["b"])
    with pytest.raises(ChatError):
        chat.add_member(member, group.channel_id, "d")
    with pytest.raises(ChatError):
        chat.history(stranger, group.channel_id)
    with pytest.raises(ChatError):
        run(chat.send(stranger, group.channel_id, "intrusion", "bad"))
    assert chat.add_member(owner, group.channel_id, "b") == group
    users = [str(i) for i in range(99)]
    full = chat.create_group(owner, users)
    assert len(full.members) == 100
    with pytest.raises(ChatError):
        chat.add_member(owner, full.channel_id, "overflow")
    with pytest.raises(ChatError):
        chat.create_group(owner, users + ["overflow"])


def test_atomic_persistence_and_restart(tmp_path):
    path = tmp_path / "chat.sqlite3"
    store = SQLiteMessageStore(path)
    service = ChatService(store)
    a = login(service, "a")
    channel = service.create_direct(a, "b")
    sent = run(service.send(a, channel.channel_id, "persistent", "retry"))
    store.close()
    reopened = SQLiteMessageStore(path)
    try:
        service = ChatService(reopened)
        a, b = login(service, "a"), login(service, "b")
        assert service.sync(b) == [sent]
        assert service.history(a, channel.channel_id) == [sent]
        assert run(service.send(a, channel.channel_id, "persistent", "retry")) == sent
        next_message = run(service.send(a, channel.channel_id, "next", "next"))
        assert next_message.message_id > sent.message_id
    finally:
        reopened.close()


def test_retry_deduplication_conflict_and_push(chat):
    a = login(chat, "a")
    channel = chat.create_direct(a, "b")
    sent = run(chat.send(a, channel.channel_id, "hello", "key"))
    assert run(chat.send(a, channel.channel_id, "hello", "key")) == sent
    assert len(chat.notifier.notifications) == 1
    assert chat.sync(a) == [sent]
    with pytest.raises(ChatError):
        run(chat.send(a, channel.channel_id, "different", "key"))
    other = chat.create_direct(a, "c")
    with pytest.raises(ChatError):
        run(chat.send(a, other.channel_id, "hello", "key"))


def test_transaction_rolls_back_message_if_inbox_insert_fails(chat):
    a = login(chat, "a")
    channel = chat.create_direct(a, "b")
    chat.store.db.execute("CREATE TRIGGER fail_inbox BEFORE INSERT ON inbox "
                          "BEGIN SELECT RAISE(ABORT, 'inbox failed'); END")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        run(chat.send(a, channel.channel_id, "hello", "key"))
    assert chat.store.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert chat.notifier.notifications == []


@pytest.mark.parametrize("content", ["", "   ", None, 42, "x" * 100_000],
                         ids=["empty", "blank", "null", "number", "too-long"])
def test_message_validation(chat, content):
    a = login(chat, "a")
    channel = chat.create_direct(a, "b")
    with pytest.raises(ChatError):
        run(chat.send(a, channel.channel_id, content, "key"))
    assert chat.sync(a) == []


def test_unicode_limit(chat):
    a = login(chat, "a")
    channel = chat.create_direct(a, "b")
    content = "你" * 99_999
    assert run(chat.send(a, channel.channel_id, content, "key")).content == content


def test_multidevice_and_cursor_pagination(chat):
    a = login(chat, "a")
    token = chat.auth.issue("b")
    phone, laptop = chat.connect(token, "phone"), chat.connect(token, "laptop")
    channel = chat.create_direct(a, "b")
    messages = [run(chat.send(a, channel.channel_id, str(i), str(i))) for i in range(3)]
    assert [phone.queue.get_nowait()["message"]["message_id"] for _ in range(3)] == [m.message_id for m in messages]
    assert laptop.queue.qsize() == 3
    page = chat.sync(phone, limit=2)
    assert page == messages[:2]
    assert chat.sync(phone, after=page[-1].message_id) == messages[2:]
    assert chat.sync(laptop) == messages
    assert chat.sync(login(chat, "outsider")) == []


@pytest.mark.parametrize("after,limit", [(-1, 1), (0, 0), (0, 1001), ("0", 10)])
def test_invalid_pagination(chat, after, limit):
    with pytest.raises(ChatError):
        chat.sync(login(chat, "a"), after, limit)


def test_join_does_not_leak_previous_history(chat):
    a, c = login(chat, "a"), login(chat, "c")
    group = chat.create_group(a, ["b"])
    run(chat.send(a, group.channel_id, "before", "1"))
    chat.add_member(a, group.channel_id, "c")
    new = run(chat.send(a, group.channel_id, "after", "2"))
    assert chat.history(c, group.channel_id) == [new]
    assert chat.sync(c) == [new]


def test_concurrent_send_and_idempotency(chat):
    a = login(chat, "a")
    channel = chat.create_direct(a, "b")

    async def scenario():
        return await asyncio.gather(*(chat.send(a, channel.channel_id, "same", "key") for _ in range(20)))

    messages = run(scenario())
    assert len({m.message_id for m in messages}) == 1
    assert len(chat.sync(a)) == 1
    assert len(chat.notifier.notifications) == 1


def test_slow_client_recovers_from_durable_inbox():
    service = ChatService(config=ChatConfig(session_queue_size=1))
    try:
        a, b = login(service, "a"), login(service, "b")
        channel = service.create_direct(a, "b")
        run(service.send(a, channel.channel_id, "one", "1"))
        run(service.send(a, channel.channel_id, "two", "2"))
        assert b.closed
        assert b.queue.get_nowait()["type"] == "closed"
        recovered = login(service, "b")
        assert [m.content for m in service.sync(recovered)] == ["one", "two"]
    finally:
        service.store.close()


def test_presence_heartbeats_multiple_devices_and_subscriptions():
    now = [0.0]
    service = ChatService(clock=lambda: now[0])
    try:
        watcher = login(service, "watcher")
        assert service.subscribe_presence(watcher, ["a"]) == {"a": False}
        phone, laptop = login(service, "a"), login(service, "a", "laptop")
        assert watcher.queue.get_nowait() == {"type": "presence", "user_id": "a", "online": True}
        service.disconnect(phone)
        assert service.is_online("a")
        assert watcher.queue.empty()
        now[0] = 20
        service.heartbeat(laptop)
        service.heartbeat(watcher)
        now[0] = 49
        service.heartbeat(watcher)
        assert service.is_online("a")
        now[0] = 50
        service.expire_sessions()
        assert not service.is_online("a")
        assert watcher.queue.get_nowait()["online"] is False
        with pytest.raises(AuthenticationError):
            service.heartbeat(laptop)
    finally:
        service.store.close()


def test_push_failure_preserves_message(chat):
    class BrokenPush:
        async def notify(self, user_id, message):
            raise RuntimeError("provider unavailable")

    chat.notifier = BrokenPush()
    a = login(chat, "a")
    channel = chat.create_direct(a, "b")
    sent = run(chat.send(a, channel.channel_id, "hello", "key"))
    assert chat.notification_failures == [("b", sent.message_id, "RuntimeError")]
    assert chat.sync(login(chat, "b")) == [sent]


def test_live_order_is_preserved_during_slow_push(chat):
    class BlockingPush:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def notify(self, user_id, message):
            if message.client_message_id == "1":
                self.started.set()
                await self.release.wait()

    async def scenario():
        a, c = login(chat, "a"), login(chat, "c")
        channel = chat.create_group(a, ["b", "c"])
        push = BlockingPush()
        chat.notifier = push
        first = asyncio.create_task(chat.send(a, channel.channel_id, "first", "1"))
        await push.started.wait()
        await chat.send(a, channel.channel_id, "second", "2")
        push.release.set()
        await first
        assert [c.queue.get_nowait()["message"]["content"] for _ in range(2)] == ["first", "second"]

    run(scenario())


def test_discovery_region_capacity_and_failover():
    registry = ServiceDiscovery()
    registry.register(Server("one", "ws://one", "east", 100, 90))
    registry.register(Server("two", "ws://two", "west", 100, 10))
    assert registry.select("east").server_id == "one"
    assert registry.select().server_id == "two"
    registry.register(Server("one", "ws://one", "east", 100, 100))
    assert registry.select("east").server_id == "two"
    registry.unregister("two")
    with pytest.raises(ChatError):
        registry.select()


@pytest.mark.parametrize("kwargs", [{"max_group_members": 101}, {"max_message_length": 100000},
                                    {"heartbeat_timeout": 0}, {"session_queue_size": 0}])
def test_config_validation(kwargs):
    with pytest.raises(ValueError):
        ChatConfig(**kwargs)


@pytest.mark.parametrize("key", ["", None, 5, "x" * 201], ids=["empty", "null", "number", "long"])
def test_invalid_retry_key(chat, key):
    a = login(chat, "a")
    channel = chat.create_direct(a, "b")
    with pytest.raises(ChatError):
        run(chat.send(a, channel.channel_id, "message", key))
    assert chat.sync(a) == []


def test_presence_overflow_does_not_leave_closed_subscriptions():
    service = ChatService(config=ChatConfig(session_queue_size=1))
    try:
        watcher = login(service, "watcher")
        service.subscribe_presence(watcher, ["a", "b"])
        login(service, "a")
        login(service, "b")
        assert watcher.closed
        assert watcher not in service._subscriptions
    finally:
        service.store.close()


def test_online_recipient_does_not_trigger_push(chat):
    a, b = login(chat, "a"), login(chat, "b")
    channel = chat.create_direct(a, "b")
    run(chat.send(a, channel.channel_id, "hello", "1"))
    assert b.queue.qsize() == 1
    assert chat.notifier.notifications == []
