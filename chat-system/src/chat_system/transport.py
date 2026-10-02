"""Optional real WebSocket transport; importing this module needs no extra dependency."""
import asyncio
import json
import inspect
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict

from .models import AuthenticationError, ChatError


async def resolve(value):
    """Accept existing synchronous core methods and async distributed methods."""
    return await value if inspect.isawaitable(value) else value


class WebSocketChatServer:
    def __init__(self, service, auth_timeout=10.0, cleanup_interval=1.0):
        if auth_timeout <= 0 or cleanup_interval <= 0:
            raise ValueError("timeouts must be positive")
        self.service = service
        self.auth_timeout = auth_timeout
        self.cleanup_interval = cleanup_interval
        self._owned_sessions = set()

    async def dispatch(self, session, request):
        op = request["op"]
        service = self.service
        if op == "heartbeat":
            await resolve(service.heartbeat(session))
            return {"online": True}
        if op == "direct":
            return asdict(await resolve(service.create_direct(session, request["recipient_id"])))
        if op == "group":
            members = request["members"]
            if not isinstance(members, list) or len(members) > 100:
                raise ChatError("invalid members")
            return asdict(await resolve(service.create_group(session, members)))
        if op == "add_member":
            return asdict(await resolve(service.add_member(session, request["channel_id"], request["user_id"])))
        if op == "send":
            return asdict(await service.send(session, request["channel_id"], request["content"],
                                              request["client_message_id"]))
        if op in ("sync", "history"):
            after, limit = request.get("after", 0), request.get("limit", 100)
            if op == "sync":
                messages = await resolve(service.sync(session, after, limit))
            else:
                messages = await resolve(service.history(session, request["channel_id"], after, limit))
            # Page size is also bounded in bytes; Unicode content can be much larger
            # than its character count. Return a smaller page with a valid cursor.
            page, byte_count = [], 0
            for message in messages:
                value = asdict(message)
                size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
                if page and byte_count + size > 1_900_000:
                    break
                page.append(value)
                byte_count += size
            return {"messages": page,
                    "next_cursor": page[-1]["message_id"] if page else after}
        if op == "subscribe_presence":
            users = request["user_ids"]
            if not isinstance(users, list) or len(users) > 100:
                raise ChatError("invalid subscriptions")
            return await resolve(service.subscribe_presence(session, users))
        if op == "logout":
            await resolve(service._require(session))
            await resolve(service.disconnect(session))
            return {"logged_out": True}
        raise ChatError("unknown operation")

    async def handler(self, websocket):
        from websockets.exceptions import ConnectionClosed

        session = None
        pump = None
        send_lock = asyncio.Lock()
        deadline = asyncio.get_running_loop().time() + self.auth_timeout

        async def write(value):
            async with send_lock:
                await websocket.send(json.dumps(value, ensure_ascii=False))

        async def events():
            while True:
                event = await session.queue.get()
                await write(event)
                if event["type"] == "closed":
                    await websocket.close(code=1000, reason="reconnect and sync")
                    return

        try:
            while True:
                if session is None:
                    timeout = max(0, deadline - asyncio.get_running_loop().time())
                    raw = await asyncio.wait_for(websocket.recv(), timeout)
                else:
                    raw = await websocket.recv()
                request_id = None
                try:
                    if not isinstance(raw, str):
                        raise ChatError("text JSON required")
                    request = json.loads(raw)
                    if not isinstance(request, dict) or not isinstance(request.get("op"), str):
                        raise ChatError("JSON object with operation required")
                    request_id = request.get("request_id")
                    if not (request_id is None or isinstance(request_id, (str, int))):
                        request_id = None
                        raise ChatError("invalid request ID")
                    if session is None:
                        if request["op"] != "login":
                            raise AuthenticationError("login required")
                        session = await resolve(self.service.connect(request["token"], request["device_id"]))
                        self._owned_sessions.add(session)
                        result = {"user_id": session.user_id, "device_id": session.device_id}
                    else:
                        result = await self.dispatch(session, request)
                    await write({"type": "response", "request_id": request_id, "ok": True, "result": result})
                    if request["op"] == "logout":
                        return
                    if pump is None:
                        pump = asyncio.create_task(events())
                except (ChatError, KeyError, TypeError, ValueError):
                    await write({"type": "response", "request_id": request_id,
                                 "ok": False, "error": "invalid or unauthorized request"})
                except Exception:
                    # Shared backend errors must not masquerade as successful volatile writes.
                    await write({"type": "response", "request_id": request_id,
                                 "ok": False, "error": "service unavailable; rediscover and retry"})
        except asyncio.TimeoutError:
            await websocket.close(code=1008, reason="login timeout")
        except ConnectionClosed:
            pass
        finally:
            if pump is not None:
                pump.cancel()
                with suppress(asyncio.CancelledError, ConnectionClosed):
                    await pump
            # Preserve online grace for transport loss. The cleanup loop expires this session.

    async def _cleanup(self):
        while True:
            await asyncio.sleep(self.cleanup_interval)
            try:
                await resolve(self.service.expire_sessions())
            except Exception:
                # A temporary broker outage must not permanently stop expiry cleanup.
                continue
            self._owned_sessions = {s for s in self._owned_sessions if not s.closed}

    @asynccontextmanager
    async def running(self, host="127.0.0.1", port=8765, **options):
        """Serve until the context exits; port=0 selects an ephemeral test port."""
        from websockets.asyncio.server import serve

        options.setdefault("max_size", 2_000_000)
        cleanup = asyncio.create_task(self._cleanup())
        try:
            async with serve(self.handler, host, port, **options) as server:
                yield server
        finally:
            cleanup.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup
            for session in list(self._owned_sessions):
                await resolve(self.service.disconnect(session))
            self._owned_sessions.clear()
