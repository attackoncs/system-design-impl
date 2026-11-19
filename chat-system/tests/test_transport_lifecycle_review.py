"""Login and logout ordering regressions at the WebSocket boundary."""

import asyncio
import json

import pytest

pytest.importorskip("websockets")
import chat_system
import websockets.asyncio.client
import websockets.exceptions
from chat_system.client import ReconnectingChatClient
from chat_system.distributed import DistributedChatService
from chat_system.transport import WebSocketChatServer


class AdmissionBackend:
    """Keep the real distributed lifecycle while controlling external I/O."""

    def __init__(self, pause_auth=False):
        self.auth_started = asyncio.Event()
        self.auth_release = asyncio.Event()
        if not pause_auth:
            self.auth_release.set()
        self.admissions = set()

    async def authenticate(self, _token):
        self.auth_started.set()
        await self.auth_release.wait()
        return "alice"

    async def admit(self, _user, _device, session, *_args):
        self.admissions.add(session)
        return True

    async def session_operation(
        self, _user, _device, session, _node, operation="check", ttl=30, **_options
    ):
        if operation == "close":
            # Model network I/O after _local_close queues the closed event.
            await asyncio.sleep(0)
            self.admissions.discard(session)
        return True


def test_distributed_logout_replies_before_its_closed_event():
    async def scenario():
        backend = AdmissionBackend()
        service = DistributedChatService(backend, "node", "ws://127.0.0.1")
        service.started = True
        adapter = WebSocketChatServer(service, cleanup_interval=100)
        async with adapter.running(port=0) as server:
            endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            client = ReconnectingChatClient(
                "http://127.0.0.1/discover", "token", "phone", max_reconnects=1
            )

            async def discover():
                return endpoint

            client.discover = discover
            try:
                assert await client.call("heartbeat") == {"online": True}
                assert await client.call("logout") == {"logged_out": True}
                assert not backend.admissions
            finally:
                await client.close()

    asyncio.run(scenario())


def test_login_deadline_closes_socket_before_admission_cleanup_finishes():
    async def scenario():
        backend = AdmissionBackend(pause_auth=True)
        service = DistributedChatService(backend, "node", "ws://127.0.0.1")
        service.started = True
        adapter = WebSocketChatServer(service, auth_timeout=0.01, cleanup_interval=100)
        async with adapter.running(port=0) as server:
            endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            async with websockets.asyncio.client.connect(endpoint) as socket:
                try:
                    await socket.send(
                        json.dumps(
                            {"op": "login", "token": "token", "device_id": "phone"}
                        )
                    )
                    await asyncio.wait_for(backend.auth_started.wait(), 1)
                    with pytest.raises(websockets.exceptions.ConnectionClosed):
                        await asyncio.wait_for(socket.recv(), 0.5)
                    assert socket.close_code == 1008
                    assert not backend.auth_release.is_set()
                finally:
                    backend.auth_release.set()
        assert not service._sessions
        assert not backend.admissions

    asyncio.run(scenario())


def test_repeated_handler_cancellation_releases_a_late_generic_session():
    async def scenario():
        class LateService(chat_system.ChatService):
            def __init__(self):
                super().__init__()
                self.connect_started = asyncio.Event()
                self.connect_canceled = asyncio.Event()
                self.connect_release = asyncio.Event()

            async def connect(self, token, device):
                session = super().connect(token, device)
                self.connect_started.set()
                try:
                    await self.connect_release.wait()
                except asyncio.CancelledError:
                    self.connect_canceled.set()
                    await self.connect_release.wait()
                return session

        service = LateService()
        token = service.auth.issue("alice")

        class Socket:
            async def recv(self):
                return json.dumps({"op": "login", "token": token, "device_id": "phone"})

            async def send(self, _payload):
                return None

            async def close(self, **_options):
                return None

        handler = asyncio.create_task(WebSocketChatServer(service).handler(Socket()))
        try:
            await asyncio.wait_for(service.connect_started.wait(), 1)
            handler.cancel()
            await asyncio.wait_for(service.connect_canceled.wait(), 1)
            handler.cancel()
            service.connect_release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(handler, 1)
            assert not service.is_online("alice")
        finally:
            service.connect_release.set()
            handler.cancel()
            await asyncio.gather(handler, return_exceptions=True)
            service.store.close()

    asyncio.run(scenario())


def test_rejected_logout_keeps_live_events_working():
    async def scenario():
        class RejectLogoutService(chat_system.ChatService):
            reject = False

            def validate_session(self, session):
                if self.reject:
                    self.reject = False
                    raise chat_system.ChatError("try again")
                return super().validate_session(session)

        service = RejectLogoutService()
        adapter = WebSocketChatServer(service)
        token = service.auth.issue("alice")
        try:
            async with adapter.running(port=0) as server:
                endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                async with websockets.asyncio.client.connect(endpoint) as socket:
                    await socket.send(
                        json.dumps(
                            {"op": "login", "token": token, "device_id": "phone"}
                        )
                    )
                    assert json.loads(await socket.recv())["ok"] is True
                    service.reject = True
                    await socket.send('{"op":"logout","request_id":2}')
                    assert json.loads(await socket.recv())["ok"] is False
                    session = service._sessions[("alice", "phone")]
                    session.queue.put_nowait({"type": "presence", "online": True})
                    assert json.loads(await asyncio.wait_for(socket.recv(), 1)) == {
                        "type": "presence",
                        "online": True,
                    }
        finally:
            service.store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_send", [False, True])
def test_logout_waits_for_an_inflight_event_before_rejecting(fail_send):
    async def scenario():
        class RejectLogoutService(chat_system.ChatService):
            reject = False

            def validate_session(self, session):
                if self.reject:
                    raise chat_system.ChatError("logout rejected")
                return super().validate_session(session)

        service = RejectLogoutService()
        token = service.auth.issue("alice")

        class PausedSocket:
            def __init__(self):
                self.requests = asyncio.Queue()
                self.sent = asyncio.Queue()
                self.event_started = asyncio.Event()
                self.event_release = asyncio.Event()
                self.logout_received = asyncio.Event()
                self.closed = asyncio.Event()
                self.close_code = None

            async def recv(self):
                request = await self.requests.get()
                if request is None:
                    raise websockets.exceptions.ConnectionClosedOK(None, None)
                if request["op"] == "logout":
                    self.logout_received.set()
                return json.dumps(request)

            async def send(self, payload):
                if self.closed.is_set():
                    raise websockets.exceptions.ConnectionClosedOK(None, None)
                value = json.loads(payload)
                if value.get("type") == "presence" and value["online"]:
                    self.event_started.set()
                    await self.event_release.wait()
                    if fail_send:
                        raise OSError("event delivery failed")
                self.sent.put_nowait(value)

            async def close(self, code=1000, **_options):
                self.close_code = code
                self.closed.set()
                self.requests.put_nowait(None)

        socket = PausedSocket()
        handler = asyncio.create_task(WebSocketChatServer(service).handler(socket))
        try:
            socket.requests.put_nowait(
                {"op": "login", "token": token, "device_id": "phone"}
            )
            assert (await asyncio.wait_for(socket.sent.get(), 1))["ok"] is True
            session = service._sessions[("alice", "phone")]
            session.queue.put_nowait({"type": "presence", "online": True})
            await asyncio.wait_for(socket.event_started.wait(), 1)
            service.reject = True
            socket.requests.put_nowait({"op": "logout", "request_id": 2})
            await asyncio.wait_for(socket.logout_received.wait(), 1)
            # The handler has requested pump cancellation; release its write.
            await asyncio.sleep(0)
            socket.event_release.set()
            if fail_send:
                await asyncio.wait_for(socket.closed.wait(), 1)
                assert socket.close_code == 1011
            else:
                assert await asyncio.wait_for(socket.sent.get(), 1) == {
                    "type": "presence",
                    "online": True,
                }
                response = await asyncio.wait_for(socket.sent.get(), 1)
                assert response["request_id"] == 2
                assert response["ok"] is False
                session.queue.put_nowait({"type": "presence", "online": False})
                assert await asyncio.wait_for(socket.sent.get(), 1) == {
                    "type": "presence",
                    "online": False,
                }
        finally:
            socket.event_release.set()
            handler.cancel()
            await asyncio.gather(handler, return_exceptions=True)
            service.store.close()

    asyncio.run(scenario())
