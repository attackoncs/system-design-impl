import asyncio
import json

import pytest

pytest.importorskip("websockets")
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from chat_system import ChatConfig, ChatService
from chat_system.transport import WebSocketChatServer


async def request(ws, request_id, op, **parameters):
    await ws.send(json.dumps({"request_id": request_id, "op": op, **parameters}))
    while True:
        event = json.loads(await asyncio.wait_for(ws.recv(), 3))
        if event.get("type") == "response" and event.get("request_id") == request_id:
            return event


def test_websocket_delivery_sync_errors_and_logout():
    async def scenario():
        service = ChatService()
        alice_token, bob_token = service.auth.issue("alice"), service.auth.issue("bob")
        adapter = WebSocketChatServer(service)
        try:
            async with adapter.running(port=0) as server:
                url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                async with connect(url) as a, connect(url) as b:
                    assert not (await request(a, 0, "sync"))["ok"]
                    assert (await request(a, 1, "login", token=alice_token, device_id="phone"))["ok"]
                    assert (await request(b, 1, "login", token=bob_token, device_id="laptop"))["ok"]
                    result = await request(a, 2, "direct", recipient_id="bob")
                    channel = result["result"]["channel_id"]
                    sent = await request(a, 3, "send", channel_id=channel, content="你好",
                                         client_message_id="unique-1")
                    assert sent["ok"]
                    live = json.loads(await asyncio.wait_for(b.recv(), 3))
                    assert live["type"] == "message"
                    assert live["message"]["content"] == "你好"
                    synced = await request(b, 4, "sync", after=0, limit=1)
                    assert synced["result"]["messages"] == [sent["result"]]
                    assert synced["result"]["next_cursor"] == sent["result"]["message_id"]
                    assert (await request(b, 5, "heartbeat"))["ok"]
                    await b.send("not JSON")
                    malformed = json.loads(await asyncio.wait_for(b.recv(), 3))
                    assert malformed["ok"] is False
                    assert not (await request(b, 6, "send", channel_id=channel, content="x"))["ok"]
                    assert not (await request(b, 7, "login", token=bob_token, device_id="other"))["ok"]
                    assert (await request(b, 8, "logout"))["ok"]
                    assert not service.is_online("bob")
            assert not service.is_online("alice")
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_transport_grace_then_heartbeat_expiry():
    async def scenario():
        now = [0.0]
        service = ChatService(clock=lambda: now[0])
        adapter = WebSocketChatServer(service)
        token = service.auth.issue("a")
        try:
            async with adapter.running(port=0) as server:
                url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                async with connect(url) as ws:
                    await request(ws, 1, "login", token=token, device_id="phone")
                assert service.is_online("a")
                now[0] = 30
                service.expire_sessions()
                assert not service.is_online("a")
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_login_timeout():
    async def scenario():
        service = ChatService()
        adapter = WebSocketChatServer(service, auth_timeout=0.05)
        try:
            async with adapter.running(port=0) as server:
                url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                async with connect(url) as ws:
                    with pytest.raises(ConnectionClosed):
                        await asyncio.wait_for(ws.recv(), 2)
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_background_cleanup_closes_expired_socket():
    async def scenario():
        service = ChatService(config=ChatConfig(heartbeat_timeout=0.1))
        adapter = WebSocketChatServer(service, cleanup_interval=0.01)
        try:
            async with adapter.running(port=0) as server:
                url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                async with connect(url) as ws:
                    await request(ws, 1, "login", token=service.auth.issue("a"), device_id="phone")
                    event = json.loads(await asyncio.wait_for(ws.recv(), 2))
                    assert event["type"] == "closed"
                    assert not service.is_online("a")
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_group_protocol_presence_and_membership_isolation():
    async def scenario():
        service = ChatService()
        adapter = WebSocketChatServer(service)
        try:
            async with adapter.running(port=0) as server:
                url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                async with connect(url) as a, connect(url) as b:
                    await request(a, 1, "login", token=service.auth.issue("a"), device_id="phone")
                    snapshot = await request(a, 2, "subscribe_presence", user_ids=["b"])
                    assert snapshot["result"] == {"b": False}
                    await request(b, 1, "login", token=service.auth.issue("b"), device_id="phone")
                    presence = json.loads(await asyncio.wait_for(a.recv(), 3))
                    assert presence["type"] == "presence" and presence["online"]
                    group = await request(a, 3, "group", members=["b"])
                    channel = group["result"]["channel_id"]
                    denied = await request(b, 2, "add_member", channel_id=channel, user_id="c")
                    assert not denied["ok"]
                    added = await request(a, 4, "add_member", channel_id=channel, user_id="c")
                    assert added["result"]["members"] == ["a", "b", "c"]
                    sent = await request(a, 5, "send", channel_id=channel,
                                         content="group hello", client_message_id="1")
                    history = await request(b, 3, "history", channel_id=channel)
                    assert history["result"]["messages"] == [sent["result"]]
                    invalid = await request(a, 6, "group", members="wrong")
                    assert not invalid["ok"]
        finally:
            service.store.close()

    asyncio.run(scenario())


def test_unicode_history_pages_are_bounded_in_bytes():
    async def scenario():
        service = ChatService()
        a = service.connect(service.auth.issue("a"), "phone")
        b = service.connect(service.auth.issue("b"), "phone")
        direct = service.create_direct(a, "b")
        try:
            for i in range(6):
                await service.send(a, direct.channel_id, "😀" * 99_999, str(i))
            adapter = WebSocketChatServer(service)
            page = await adapter.dispatch(b, {"op": "sync", "limit": 1000})
            assert 0 < len(page["messages"]) < 6
            assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) < 2_000_000
            remaining = await adapter.dispatch(b, {"op": "sync", "after": page["next_cursor"]})
            assert len(page["messages"]) + len(remaining["messages"]) == 6
        finally:
            service.store.close()

    asyncio.run(scenario())
