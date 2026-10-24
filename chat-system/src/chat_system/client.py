"""Discover/reconnect client with explicit durable inbox acknowledgement."""

import asyncio
import json
import math
import os
import pathlib
import tempfile
from typing import Any, Optional
import urllib.parse
import urllib.request

import chat_system.models

try:
    import websockets.asyncio.client
    import websockets.exceptions
except ImportError:
    websockets = None  # Optional WebSocket extra.


class ReconnectingChatClient:
    """A serial WebSocket client with rediscovery and explicit inbox
    acknowledgement.
    """

    def __init__(
        self,
        discovery_url: str,
        token: str,
        device_id: str,
        region: Optional[str] = None,
        cursor_file=None,
        timeout: float = 5,
        retry_delay: float = 0.2,
        max_reconnects: int = 30,
    ):
        for value, allow_zero in ((timeout, False), (retry_delay, True)):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                or (not allow_zero and value == 0)
            ):
                raise ValueError("invalid reconnect settings")
        if type(max_reconnects) is not int or max_reconnects < 1:
            raise ValueError("invalid reconnect settings")
        parsed = urllib.parse.urlsplit(discovery_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("discovery URL must use HTTP or HTTPS")
        self.discovery_url, self.token, self.device_id, self.region = (
            discovery_url,
            token,
            device_id,
            region,
        )
        self.cursor_file = pathlib.Path(cursor_file) if cursor_file else None
        self.cursor = 0
        if self.cursor_file and self.cursor_file.exists():
            self.cursor = int(self.cursor_file.read_text(encoding="utf-8"))
            if self.cursor < 0:
                raise ValueError("invalid saved cursor")
        self.timeout, self.retry_delay, self.max_reconnects = (
            timeout,
            retry_delay,
            max_reconnects,
        )
        self._ws = None
        self._lock = asyncio.Lock()
        self._request_id = 0
        self._pending_page = None
        self.endpoint = None

    async def discover(self) -> str:
        """Fetch and return an endpoint from the configured discovery URL."""
        url = self.discovery_url
        if self.region:
            parsed = urllib.parse.urlsplit(url)
            query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            query.append(("region", self.region))
            url = urllib.parse.urlunsplit(
                parsed._replace(query=urllib.parse.urlencode(query))
            )

        def fetch():
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                payload = response.read(8193)
            if len(payload) > 8192:
                raise chat_system.models.ChatError("discovery response is too large")
            try:
                endpoint = json.loads(payload)["url"]
                if not isinstance(endpoint, str):
                    raise ValueError("invalid endpoint")
                parsed = urllib.parse.urlsplit(endpoint)
                if (
                    parsed.scheme not in ("ws", "wss")
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.fragment
                ):
                    raise ValueError("invalid endpoint")
            except (ValueError, KeyError, TypeError) as error:
                raise chat_system.models.ChatError(
                    "invalid discovery response"
                ) from error
            return endpoint

        return await asyncio.to_thread(fetch)

    async def close(self) -> None:
        """Close the active WebSocket connection, if any."""
        websocket, self._ws = self._ws, None
        if websocket is not None:
            await websocket.close()

    async def _request(self, op: str, **parameters: Any) -> Any:
        self._request_id += 1
        request_id = self._request_id
        websocket = self._ws

        async def exchange():
            await websocket.send(
                json.dumps(
                    {"op": op, "request_id": request_id, **parameters},
                    ensure_ascii=False,
                )
            )
            while True:
                try:
                    event = json.loads(await websocket.recv())
                    if not isinstance(event, dict):
                        raise ValueError("JSON object required")
                    if event.get("type") == "closed":
                        raise ConnectionError("session closed; rediscover")
                    if (
                        event.get("type") != "response"
                        or event.get("request_id") != request_id
                    ):
                        # Live events are hints. Only an ordered sync page can
                        # advance the durable inbox cursor.
                        continue
                    if type(event["ok"]) is not bool:
                        raise ValueError("invalid status")
                    if not event["ok"]:
                        error = event["error"]
                        if not isinstance(error, str):
                            raise ValueError("invalid error")
                        if "service unavailable" in error:
                            raise ConnectionError("chat service unavailable")
                        raise chat_system.models.ChatError(error)
                    return event["result"]
                except (ValueError, KeyError, TypeError) as error:
                    raise chat_system.models.ChatError(
                        "invalid chat server response"
                    ) from error

        # One deadline covers send and every intervening live event. A busy
        # server cannot indefinitely postpone a missing response.
        return await asyncio.wait_for(exchange(), self.timeout)

    async def call(self, op: str, **parameters: Any) -> Any:
        """Send an operation, retrying transient connection failures.

        Args:
            op: Chat protocol operation name.
            **parameters: Operation fields encoded in the JSON request.

        Returns:
            The result field of the matching successful response.

        Raises:
            ChatError: The server rejects the operation, or group creation has
                an uncertain result that cannot safely be replayed.
            OSError: Reconnection attempts are exhausted by an I/O failure.
        """
        if websockets is None:
            raise ImportError("Install the websocket extra")

        async with self._lock:
            for attempt in range(self.max_reconnects):
                requested = False
                try:
                    if self._ws is None:
                        self.endpoint = await self.discover()
                        self._ws = await websockets.asyncio.client.connect(
                            self.endpoint,
                            open_timeout=self.timeout,
                            max_size=2_000_000,
                            max_queue=16,
                        )
                        await self._request(
                            "login", token=self.token, device_id=self.device_id
                        )
                    requested = True
                    return await self._request(op, **parameters)
                except (
                    OSError,
                    websockets.exceptions.ConnectionClosed,
                    asyncio.TimeoutError,
                    ConnectionError,
                ):
                    await self.close()
                    if requested and op == "group":
                        # Group creation has no idempotency key. Replaying an
                        # uncertain request could create a second group.
                        raise chat_system.models.ChatError(
                            "group creation outcome unknown; reconnect before retrying"
                        ) from None
                    if attempt + 1 == self.max_reconnects:
                        raise
                    await asyncio.sleep(self.retry_delay)
                except chat_system.models.ChatError:
                    await self.close()
                    raise
                except asyncio.CancelledError:
                    # The canceled request may still have reached the server.
                    # Discard its transport before a later call can reuse it.
                    await self.close()
                    raise

    async def receive(self, limit: int = 100) -> list[dict]:
        """Return up to limit inbox messages after the last acknowledged
        cursor.
        """
        result = await self.call("sync", after=self.cursor, limit=limit)
        try:
            messages = result["messages"]
            ids = self._page_ids(messages)
            expected = ids[-1] if ids else self.cursor
            if (
                type(result["next_cursor"]) is not int
                or result["next_cursor"] != expected
            ):
                raise ValueError("invalid inbox cursor")
            if ids and ids[0] <= self.cursor:
                raise ValueError("invalid inbox cursor")
        except (ValueError, KeyError, TypeError) as error:
            await self.close()
            raise chat_system.models.ChatError("invalid inbox page") from error
        self._pending_page = ids
        return messages

    @staticmethod
    def _page_ids(messages):
        if not isinstance(messages, list):
            raise ValueError("message page must be a list")
        ids = tuple(message["message_id"] for message in messages)
        if any(type(value) is not int or value <= 0 for value in ids):
            raise ValueError("invalid message ID")
        if any(first >= second for first, second in zip(ids, ids[1:])):
            raise ValueError("message page must be ordered")
        return ids

    def acknowledge(self, messages: list[dict]) -> None:
        """Call only after processing a complete receive() page
        successfully. The cursor file must be dedicated to one user/device;
        partial, reordered, and arbitrary pages cannot advance the cursor.
        """
        try:
            ids = self._page_ids(messages)
        except (ValueError, KeyError, TypeError) as error:
            raise chat_system.models.ChatError(
                "invalid acknowledgement page"
            ) from error
        if not ids:
            return
        if ids != self._pending_page:
            raise chat_system.models.ChatError(
                "acknowledge the complete last receive page"
            )
        new_cursor = max(self.cursor, ids[-1])
        if self.cursor_file:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.cursor_file.parent,
                    prefix=self.cursor_file.name + ".",
                    suffix=".tmp",
                    delete=False,
                ) as file:
                    temporary = pathlib.Path(file.name)
                    file.write(str(new_cursor))
                    file.flush()
                    os.fsync(file.fileno())
                temporary.replace(self.cursor_file)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        self.cursor = new_cursor

    async def watch(self, interval=1):
        """Yield ordered sync pages; caller explicitly acknowledges each
        processed page.
        """
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not math.isfinite(interval)
            or interval <= 0
        ):
            raise ValueError("watch interval must be finite and positive")
        while True:
            await self.call("heartbeat")
            page = await self.receive()
            if page:
                yield page
            else:
                await asyncio.sleep(interval)
