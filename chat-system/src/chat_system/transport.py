"""Optional real WebSocket transport; importing this module needs no extra
dependency.
"""

import asyncio
import contextlib
import dataclasses
import inspect
import json
import logging
import math

import chat_system.models

try:
    import websockets.asyncio.server
    import websockets.exceptions
except ImportError:
    websockets = None  # Optional WebSocket extra.


async def resolve(value):
    """Accept existing synchronous core methods and async distributed
    methods.
    """
    return await value if inspect.isawaitable(value) else value


class WebSocketChatServer:
    """Translates bounded WebSocket requests into authenticated chat
    operations.
    """

    def __init__(
        self, service, auth_timeout=10.0, cleanup_interval=1.0, send_timeout=10.0
    ):
        for value in (auth_timeout, cleanup_interval, send_timeout):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("timeouts must be finite and positive")
        self.service = service
        self.auth_timeout = auth_timeout
        self.cleanup_interval = cleanup_interval
        self.send_timeout = send_timeout
        self._owned_sessions = set()

    async def _operation(self, name, *args):
        """Prefer the nonblocking adapter of an otherwise synchronous service."""
        operation = getattr(self.service, "async_" + name, None)
        if operation is None:
            operation = getattr(self.service, name)
        return await resolve(operation(*args))

    async def dispatch(self, session, request):
        op = request["op"]
        service = self.service
        if op == "heartbeat":
            await resolve(service.heartbeat(session))
            return {"online": True}
        if op == "direct":
            return dataclasses.asdict(
                await self._operation("create_direct", session, request["recipient_id"])
            )
        if op == "group":
            members = request["members"]
            if not isinstance(members, list) or len(members) > 100:
                raise chat_system.models.ChatError("invalid members")
            return dataclasses.asdict(
                await self._operation("create_group", session, members)
            )
        if op == "add_member":
            return dataclasses.asdict(
                await self._operation(
                    "add_member", session, request["channel_id"], request["user_id"]
                )
            )
        if op == "send":
            return dataclasses.asdict(
                await service.send(
                    session,
                    request["channel_id"],
                    request["content"],
                    request["client_message_id"],
                )
            )
        if op == "message":
            return dataclasses.asdict(
                await self._operation("get_message", session, request["message_id"])
            )
        if op in ("sync", "history"):
            after, limit = request.get("after", 0), request.get("limit", 100)
            if op == "sync":
                messages = await self._operation("sync", session, after, limit)
            else:
                messages = await self._operation(
                    "history", session, request["channel_id"], after, limit
                )
            # Page size is also bounded in bytes; Unicode content can be much
            # larger
            # than its character count. Return a smaller page with a valid
            # cursor.
            page, byte_count = [], 0
            for message in messages:
                value = dataclasses.asdict(message)
                size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
                if page and byte_count + size > 1_900_000:
                    break
                page.append(value)
                byte_count += size
            return {
                "messages": page,
                "next_cursor": page[-1]["message_id"] if page else after,
            }
        if op == "subscribe_presence":
            users = request["user_ids"]
            if not isinstance(users, list) or len(users) > 100:
                raise chat_system.models.ChatError("invalid subscriptions")
            return await resolve(service.subscribe_presence(session, users))
        if op == "logout":
            await resolve(service.validate_session(session))
            await resolve(service.disconnect(session))
            return {"logged_out": True}
        raise chat_system.models.ChatError("unknown operation")

    async def handler(self, websocket):
        if websockets is None:
            raise ImportError("Install the websocket extra")

        session = None
        pump = None
        send_lock = asyncio.Lock()
        deadline = asyncio.get_running_loop().time() + self.auth_timeout

        async def write(value):
            async with send_lock:
                await asyncio.wait_for(
                    websocket.send(json.dumps(value, ensure_ascii=False)),
                    self.send_timeout,
                )

        async def events():
            try:
                while True:
                    event = await session.queue.get()
                    await write(event)
                    if event["type"] == "closed":
                        await websocket.close(code=1000, reason="reconnect and sync")
                        return
            except websockets.exceptions.ConnectionClosed:
                return
            except Exception:  # pylint: disable=broad-exception-caught
                # A failed event writer must also wake the request reader.
                logging.getLogger(__name__).exception("Chat event delivery failed")
                await websocket.close(code=1011, reason="reconnect and sync")

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
                        raise chat_system.models.ChatError("text JSON required")
                    request = json.loads(raw)
                    if not isinstance(request, dict) or not isinstance(
                        request.get("op"), str
                    ):
                        raise chat_system.models.ChatError(
                            "JSON object with operation required"
                        )
                    request_id = request.get("request_id")
                    if not (
                        request_id is None
                        or (isinstance(request_id, str) and len(request_id) <= 128)
                        or (type(request_id) is int and 0 <= request_id < 2**63)
                    ):
                        request_id = None
                        raise chat_system.models.ChatError("invalid request ID")
                    if session is None:
                        if request["op"] != "login":
                            raise chat_system.models.AuthenticationError(
                                "login required"
                            )
                        session = await resolve(
                            self.service.connect(request["token"], request["device_id"])
                        )
                        self._owned_sessions.add(session)
                        result = {
                            "user_id": session.user_id,
                            "device_id": session.device_id,
                        }
                    else:
                        result = await self.dispatch(session, request)
                    await write(
                        {
                            "type": "response",
                            "request_id": request_id,
                            "ok": True,
                            "result": result,
                        }
                    )
                    if request["op"] == "logout":
                        return
                    if pump is None:
                        pump = asyncio.create_task(events())
                except (
                    chat_system.models.ChatError,
                    KeyError,
                    TypeError,
                    ValueError,
                ):
                    await write(
                        {
                            "type": "response",
                            "request_id": request_id,
                            "ok": False,
                            "error": "invalid or unauthorized request",
                        }
                    )
                # Isolate this operation; record failures before continuing.
                except Exception:  # pylint: disable=broad-exception-caught
                    # Shared backend errors must not masquerade as successful
                    # volatile writes.
                    logging.getLogger(__name__).exception(
                        "Failure isolated in %s", "handler"
                    )
                    await write(
                        {
                            "type": "response",
                            "request_id": request_id,
                            "ok": False,
                            "error": ("service unavailable; rediscover and retry"),
                        }
                    )
        except asyncio.TimeoutError:
            if session is None:
                await websocket.close(code=1008, reason="login timeout")
            else:
                await websocket.close(
                    code=1011, reason="response timeout; reconnect and sync"
                )
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            if pump is not None:
                pump.cancel()
                with contextlib.suppress(
                    asyncio.CancelledError, websockets.exceptions.ConnectionClosed
                ):
                    await pump
            # Preserve online grace for transport loss. The cleanup loop expires
            # this session.

    async def _cleanup(self):
        while True:
            await asyncio.sleep(self.cleanup_interval)
            try:
                await resolve(self.service.expire_sessions())
            # Isolate this operation; record failures before continuing.
            except Exception:  # pylint: disable=broad-exception-caught
                # A temporary broker outage must not permanently stop expiry
                # cleanup.
                logging.getLogger(__name__).exception(
                    "Failure isolated in %s", "_cleanup"
                )
                continue
            self._owned_sessions = {s for s in self._owned_sessions if not s.closed}

    @contextlib.asynccontextmanager
    async def running(self, host="127.0.0.1", port=8765, **options):
        """Serve until the context exits; port=0 selects an ephemeral test
        port.
        """
        if websockets is None:
            raise ImportError("Install the websocket extra")

        options.setdefault("max_size", 2_000_000)
        cleanup = asyncio.create_task(self._cleanup())
        try:
            async with websockets.asyncio.server.serve(
                self.handler, host, port, **options
            ) as server:
                yield server
        finally:
            cleanup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup
            for session in list(self._owned_sessions):
                try:
                    await resolve(self.service.disconnect(session))
                except Exception:  # pylint: disable=broad-exception-caught
                    # Drain every owned session even when one backend release fails.
                    logging.getLogger(__name__).exception("Chat session cleanup failed")
            self._owned_sessions.clear()
