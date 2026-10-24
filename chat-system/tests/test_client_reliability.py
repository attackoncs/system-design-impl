"""Connection and durable-cursor regressions without external services."""

import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("websockets")
import chat_system
import chat_system.client
import chat_system.transport
import websockets.asyncio.client


class ScriptedSocket:
    def __init__(self, receive):
        self.receive = receive
        self.sent = []
        self.closed = False
        self.started = asyncio.Event()

    async def send(self, payload):
        self.sent.append(json.loads(payload))
        self.started.set()

    async def recv(self):
        return await self.receive()

    async def close(self, **_options):
        self.closed = True


def make_client(**options):
    return chat_system.client.ReconnectingChatClient(
        "http://127.0.0.1/discover", "token", "phone", **options
    )


def test_response_deadline_includes_continuous_live_events():
    async def scenario():
        async def live_event():
            await asyncio.sleep(0.001)
            return '{"type":"presence","online":true}'

        client = make_client(timeout=0.025)
        client._ws = ScriptedSocket(live_event)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(client._request("heartbeat"), 0.5)
        assert len(client._ws.sent) == 1

    asyncio.run(scenario())


def test_canceled_login_discards_unconfirmed_connection(monkeypatch):
    async def scenario():
        async def pending():
            await asyncio.Future()

        socket = ScriptedSocket(pending)
        client = make_client()
        client.discover = AsyncMock(return_value="ws://127.0.0.1:12345")
        connect = AsyncMock(return_value=socket)
        monkeypatch.setattr(
            chat_system.client.websockets.asyncio.client, "connect", connect
        )
        operation = asyncio.create_task(client.call("heartbeat"))
        await asyncio.wait_for(socket.started.wait(), 1)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert socket.closed
        assert client._ws is None
        assert connect.call_args.kwargs["max_size"] == 2_000_000
        assert connect.call_args.kwargs["max_queue"] == 16

    asyncio.run(scenario())


def test_uncertain_group_creation_is_not_automatically_replayed():
    async def scenario():
        async def lost_reply():
            raise ConnectionError("reply lost after commit")

        client = make_client(max_reconnects=5, retry_delay=0)
        socket = client._ws = ScriptedSocket(lost_reply)
        with pytest.raises(chat_system.ChatError, match="outcome unknown"):
            await client.call("group", members=["bob"])
        assert len(socket.sent) == 1
        assert socket.closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '{"type":"response","request_id":1,"ok":1}',
        '{"type":"response","request_id":1,"ok":true}',
    ],
)
def test_malformed_response_closes_transport(payload):
    async def scenario():
        async def invalid():
            return payload

        client = make_client()
        socket = client._ws = ScriptedSocket(invalid)
        with pytest.raises(chat_system.ChatError, match="invalid chat server response"):
            await client.call("heartbeat")
        assert socket.closed
        assert client._ws is None

    asyncio.run(scenario())


def test_acknowledgement_must_match_the_complete_received_page(tmp_path):
    async def scenario():
        client = make_client(cursor_file=tmp_path / "phone.cursor")
        page = [{"message_id": 2}, {"message_id": 5}]
        client.call = AsyncMock(return_value={"messages": page, "next_cursor": 5})
        for arbitrary in (page, [{"message_id": 999}]):
            with pytest.raises(
                chat_system.ChatError, match="complete last receive page"
            ):
                client.acknowledge(arbitrary)
        received = await client.receive()
        for incomplete in (received[:1], received[1:], list(reversed(received))):
            with pytest.raises(chat_system.ChatError):
                client.acknowledge(incomplete)
            assert client.cursor == 0
        client.acknowledge(received)
        assert client.cursor == 5
        assert (tmp_path / "phone.cursor").read_text(encoding="utf-8") == "5"
        client.acknowledge(received)  # An acknowledgement retry is harmless.
        assert not list(tmp_path.glob("*.tmp"))

    asyncio.run(scenario())


def test_cursor_write_failure_keeps_page_available_for_retry(tmp_path):
    async def scenario():
        client = make_client(cursor_file=tmp_path / "missing" / "phone.cursor")
        page = [{"message_id": 3}]
        client.call = AsyncMock(return_value={"messages": page, "next_cursor": 3})
        await client.receive()
        with pytest.raises(OSError):
            client.acknowledge(page)
        assert client.cursor == 0
        client.cursor_file.parent.mkdir()
        client.acknowledge(page)
        assert client.cursor == 3

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "page,cursor",
    [
        ([{"message_id": 4}, {"message_id": 2}], 2),
        ([{"message_id": True}], True),
        ([{"message_id": 4}], 5),
    ],
)
def test_unordered_or_inconsistent_inbox_page_cannot_advance_cursor(page, cursor):
    async def scenario():
        client = make_client()
        client.call = AsyncMock(return_value={"messages": page, "next_cursor": cursor})
        with pytest.raises(chat_system.ChatError, match="invalid inbox page"):
            await client.receive()
        assert client.cursor == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "options",
    [
        {"timeout": float("nan")},
        {"timeout": float("inf")},
        {"timeout": True},
        {"retry_delay": float("nan")},
        {"retry_delay": -1},
        {"max_reconnects": 1.5},
        {"max_reconnects": True},
    ],
)
def test_reconnect_limits_reject_nonfinite_or_wrong_types(options):
    with pytest.raises(ValueError):
        make_client(**options)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.com",
        "ws://user:secret@example.com",
        "ws://example.com/#fragment",
        None,
    ],
)
def test_discovery_rejects_invalid_websocket_endpoint(monkeypatch, endpoint):
    monkeypatch.setattr(
        chat_system.client.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(json.dumps({"url": endpoint}).encode()),
    )
    with pytest.raises(chat_system.ChatError, match="invalid discovery response"):
        asyncio.run(make_client().discover())


def test_transport_prefers_async_local_store_adapter():
    async def scenario():
        async_sync = AsyncMock(return_value=[])
        service = SimpleNamespace(async_sync=async_sync)
        server = chat_system.transport.WebSocketChatServer(service)
        session = object()
        assert await server.dispatch(
            session, {"op": "sync", "after": 3, "limit": 2}
        ) == {"messages": [], "next_cursor": 3}
        async_sync.assert_awaited_once_with(session, 3, 2)

    asyncio.run(scenario())


def test_failed_event_writer_closes_socket_and_drains_pump():
    async def scenario():
        service = chat_system.ChatService()
        token = service.auth.issue("alice")
        login = json.dumps({"op": "login", "token": token, "device_id": "phone"})

        class FailingEventSocket:
            def __init__(self):
                self.logged_in = False
                self.closed = asyncio.Event()

            async def recv(self):
                if not self.logged_in:
                    self.logged_in = True
                    return login
                # The event pump must close the transport to wake this read.
                await self.closed.wait()
                raise asyncio.CancelledError

            async def send(self, payload):
                event = json.loads(payload)
                if event["type"] == "response":
                    session = service._sessions[("alice", "phone")]
                    session.queue.put_nowait({"type": "message"})
                else:
                    raise OSError("simulated event writer failure")

            async def close(self, **_options):
                self.closed.set()

        socket = FailingEventSocket()
        server = chat_system.transport.WebSocketChatServer(service)
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(server.handler(socket), 1)
            assert socket.closed.is_set()
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_slow_event_writer_has_a_bounded_send_deadline():
    async def scenario():
        session = SimpleNamespace(queue=asyncio.Queue())
        session.queue.put_nowait({"type": "message"})

        # The fake service returns a hashable session just like real services.
        class HashableSession:
            user_id = "alice"
            device_id = "phone"
            queue = session.queue

        session = HashableSession()
        service = SimpleNamespace(connect=lambda *_args: session)

        class SlowSocket:
            def __init__(self):
                self.reads = 0
                self.closed = asyncio.Event()

            async def recv(self):
                self.reads += 1
                if self.reads == 1:
                    return '{"op":"login","token":"token","device_id":"phone"}'
                await self.closed.wait()
                raise asyncio.CancelledError

            async def send(self, payload):
                if json.loads(payload)["type"] == "message":
                    await asyncio.Future()

            async def close(self, **_options):
                self.closed.set()

        socket = SlowSocket()
        server = chat_system.transport.WebSocketChatServer(service, send_timeout=0.01)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(server.handler(socket), 1)
        assert socket.closed.is_set()

    asyncio.run(scenario())


def test_exact_message_protocol_applies_recipient_authorization():
    async def scenario():
        service = chat_system.ChatService()
        sender = service.connect(service.auth.issue("alice"), "phone")
        channel = service.create_direct(sender, "bob")
        sent = await service.send(sender, channel.channel_id, "private", "key-1")
        server = chat_system.transport.WebSocketChatServer(service)

        async def exchange(socket, request_id, op, **parameters):
            await socket.send(
                json.dumps({"op": op, "request_id": request_id, **parameters})
            )
            while True:
                value = json.loads(await asyncio.wait_for(socket.recv(), 1))
                if value.get("request_id") == request_id:
                    return value

        try:
            async with server.running(port=0) as running:
                endpoint = f"ws://127.0.0.1:{running.sockets[0].getsockname()[1]}"
                for user, allowed in (("bob", True), ("mallory", False)):
                    async with websockets.asyncio.client.connect(endpoint) as socket:
                        await exchange(
                            socket,
                            1,
                            "login",
                            token=service.auth.issue(user),
                            device_id="phone",
                        )
                        response = await exchange(
                            socket, 2, "message", message_id=sent.message_id
                        )
                        assert response["ok"] is allowed
                        if allowed:
                            assert response["result"]["content"] == "private"
                            assert response["result"]["message_id"] == sent.message_id
        finally:
            service.store.close()

    asyncio.run(scenario())
