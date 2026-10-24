"""Local orchestration behavior under blocking IO, cancellation and fanout."""

import asyncio
import dataclasses
import threading

import pytest

from chat_system import (
    AuthenticationError,
    ChatConfig,
    ChatError,
    ChatService,
    RecordingNotifier,
    Server,
    ServiceDiscovery,
)


def login(service, user, device="phone"):
    return service.connect(service.auth.issue(user), device)


def test_presence_overflow_chain_is_iterative_and_cleans_indexes():
    service = ChatService(config=ChatConfig(session_queue_size=1))
    try:
        sessions = [login(service, f"user{i}") for i in range(1500)]
        for watcher, watched in zip(sessions, sessions[1:]):
            service.subscribe_presence(watcher, [watched.user_id])
        for session in sessions:
            session.queue.put_nowait({"type": "occupied"})
        service.disconnect(sessions[-1])
        assert all(session.closed for session in sessions)
        assert not service._sessions and not service._user_sessions
        assert not service._subscriptions and not service._watchers
    finally:
        service.store.close()


def test_session_validation_does_not_scan_unrelated_users():
    service = ChatService()
    try:
        sessions = [login(service, f"user{i}") for i in range(100)]
        authenticate = service.auth.authenticate
        calls = []

        def counted(token):
            calls.append(token)
            return authenticate(token)

        service.auth.authenticate = counted
        service.heartbeat(sessions[0])
        assert len(calls) <= 4
    finally:
        service.store.close()


def test_async_persistence_keeps_event_loop_responsive_and_live_ids_ordered():
    async def scenario():
        service = ChatService(config=ChatConfig(session_queue_size=256))
        entered, release = threading.Event(), threading.Event()
        try:
            alice, bob = login(service, "alice"), login(service, "bob")
            channel = service.create_direct(alice, "bob")
            original = service.store.append

            def slow_append(*args):
                entered.set()
                assert release.wait(5)
                return original(*args)

            service.store.append = slow_append
            task = asyncio.create_task(
                service.send(alice, channel.channel_id, "first", "first")
            )
            while not entered.is_set():
                await asyncio.sleep(0)
            service.heartbeat(bob)
            assert not task.done()
            release.set()
            first = await task
            service.store.append = original
            sent = await asyncio.gather(
                *(
                    service.send(alice, channel.channel_id, str(i), str(i))
                    for i in range(60)
                )
            )
            live = [bob.queue.get_nowait()["message"]["message_id"] for _ in range(61)]
            assert (
                live
                == sorted(live)
                == [first.message_id] + [m.message_id for m in sent]
            )
        finally:
            release.set()
            service.store.close()

    asyncio.run(scenario())


def test_cancelled_commit_finishes_before_send_lock_is_released():
    async def scenario():
        service = ChatService()
        entered, release = threading.Event(), threading.Event()
        try:
            alice = login(service, "alice")
            channel = service.create_direct(alice, "bob")
            append = service.store.append

            def slow_append(*args):
                entered.set()
                assert release.wait(5)
                return append(*args)

            service.store.append = slow_append
            task = asyncio.create_task(
                service.send(alice, channel.channel_id, "committed", "retry")
            )
            while not entered.is_set():
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(service.sync(alice)) == 1
            service.store.append = append
            retried = await service.send(
                alice, channel.channel_id, "committed", "retry"
            )
            assert service.sync(alice) == [retried]
        finally:
            release.set()
            service.store.close()

    asyncio.run(scenario())


def test_push_timeouts_bound_parallel_work_and_failure_diagnostics():
    async def scenario():
        class SlowNotifier:
            active = 0
            maximum = 0

            async def notify(self, user, message):
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                try:
                    await asyncio.sleep(10)
                finally:
                    self.active -= 1

        notifier = SlowNotifier()
        service = ChatService(
            notifier=notifier,
            config=ChatConfig(
                push_timeout=0.01, push_concurrency=3, failure_retention=5
            ),
        )
        try:
            alice = login(service, "alice")
            group = service.create_group(alice, [f"offline{i}" for i in range(20)])
            message = await asyncio.wait_for(
                service.send(alice, group.channel_id, "hello", "push"), 2
            )
            assert service.sync(alice) == [message]
            assert notifier.maximum == 3 and notifier.active == 0
            assert service.notification_failure_count == 20
            assert len(service.notification_failures) == 5
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_live_payloads_are_independent_and_revoked_devices_receive_no_message():
    async def scenario():
        service = ChatService()
        try:
            alice, phone, laptop = (
                login(service, "alice"),
                login(service, "bob"),
                login(service, "bob", "laptop"),
            )
            channel = service.create_direct(alice, "bob")
            await service.send(alice, channel.channel_id, "original", "1")
            phone.queue.get_nowait()["message"]["content"] = "changed"
            assert laptop.queue.get_nowait()["message"]["content"] == "original"
            service.auth.revoke(phone.token)
            await service.send(alice, channel.channel_id, "second", "2")
            assert phone.closed and phone.queue.get_nowait()["type"] == "closed"
            assert laptop.queue.get_nowait()["message"]["content"] == "second"
        finally:
            service.store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "values", ["alice", {"user": "a"}, [None], [["a"]], ["x"] * 101]
)
def test_presence_input_rejects_invalid_collections(values):
    service = ChatService()
    try:
        session = login(service, "watcher")
        with pytest.raises(ChatError):
            service.subscribe_presence(session, values)
    finally:
        service.store.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"heartbeat_timeout": float("nan")},
        {"heartbeat_timeout": float("inf")},
        {"max_message_length": 1.5},
        {"session_queue_size": True},
        {"push_concurrency": True},
        {"push_timeout": float("nan")},
        {"failure_retention": 0},
        {"max_group_members": 2.5},
    ],
)
def test_strict_finite_config(kwargs):
    with pytest.raises(ValueError):
        ChatConfig(**kwargs)


@pytest.mark.parametrize(
    "changes",
    [
        {"capacity": True},
        {"connections": 1.5},
        {"url": "http://example.com"},
        {"url": "ws://user:secret@example.com"},
    ],
)
def test_discovery_rejects_invalid_registration(changes):
    registry = ServiceDiscovery()
    server = dataclasses.replace(
        Server("one", "ws://localhost", "local", 100), **changes
    )
    with pytest.raises(ChatError):
        registry.register(server)


def test_recording_notifier_retention_is_bounded():
    async def scenario():
        notifier = RecordingNotifier(capacity=2)
        for i in range(5):
            await notifier.notify("user", i)
        assert notifier.notifications == [("user", 3), ("user", 4)]

    asyncio.run(scenario())


def test_revoked_presence_subscriber_is_closed_before_delivery():
    service = ChatService()
    try:
        watcher = login(service, "watcher")
        service.subscribe_presence(watcher, ["target"])
        service.auth.revoke(watcher.token)
        login(service, "target")
        assert watcher.closed
        assert watcher.queue.get_nowait()["type"] == "closed"
        assert not service._watchers
    finally:
        service.store.close()


def test_provider_concurrency_is_shared_across_messages():
    async def scenario():
        class Provider:
            active = maximum = 0

            async def notify(self, user, message):
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                try:
                    await asyncio.sleep(0.01)
                finally:
                    self.active -= 1

        provider = Provider()
        service = ChatService(notifier=provider, config=ChatConfig(push_concurrency=2))
        try:
            sender = login(service, "sender")
            group = service.create_group(sender, ["a", "b", "c", "d"])
            await asyncio.gather(
                *(
                    service.send(sender, group.channel_id, str(i), str(i))
                    for i in range(2)
                )
            )
            assert provider.maximum == 2 and provider.active == 0
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_sequential_asyncio_runs_can_reuse_idle_core_service():
    service = ChatService(config=ChatConfig(session_queue_size=100))
    try:
        sender, _ = login(service, "sender"), login(service, "recipient")
        channel = service.create_direct(sender, "recipient")

        async def batch(prefix):
            return await asyncio.gather(
                *(
                    service.send(sender, channel.channel_id, "message", f"{prefix}-{i}")
                    for i in range(5)
                )
            )

        first = asyncio.run(batch("first"))
        second = asyncio.run(batch("second"))
        assert second[0].message_id > first[-1].message_id
    finally:
        service.store.close()


def test_revocation_during_commit_never_acknowledges_old_session():
    async def scenario():
        service = ChatService()
        entered, release = threading.Event(), threading.Event()
        try:
            sender = login(service, "sender")
            channel = service.create_direct(sender, "recipient")
            append = service.store.append

            def slow(*args):
                entered.set()
                assert release.wait(5)
                return append(*args)

            service.store.append = slow
            task = asyncio.create_task(
                service.send(sender, channel.channel_id, "accepted", "key")
            )
            while not entered.is_set():
                await asyncio.sleep(0)
            service.auth.revoke(sender.token)
            release.set()
            with pytest.raises(AuthenticationError):
                await task
            current = login(service, "sender")
            assert len(service.sync(current)) == 1  # commit may precede revocation
        finally:
            release.set()
            service.store.close()

    asyncio.run(scenario())


def test_member_identity_bounds_match_token_provisioning():
    service = ChatService()
    try:
        owner = login(service, "owner")
        group = service.create_group(owner, ["member"])
        for user in [" ", "x" * 129]:
            with pytest.raises(ChatError):
                service.create_direct(owner, user)
            with pytest.raises(ChatError):
                service.add_member(owner, group.channel_id, user)
    finally:
        service.store.close()
