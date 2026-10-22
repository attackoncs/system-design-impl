"""Discover/reconnect client with explicit durable inbox acknowledgement."""

import asyncio
import json
import os
import pathlib
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
        discovery_url,
        token,
        device_id,
        region=None,
        cursor_file=None,
        timeout=5,
        retry_delay=0.2,
        max_reconnects=30,
    ):
        if timeout <= 0 or retry_delay < 0 or max_reconnects < 1:
            raise ValueError("invalid reconnect settings")
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
        self.endpoint = None

    async def discover(self):
        """Fetch and return an endpoint from the configured discovery URL."""
        url = self.discovery_url
        if self.region:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
                {"region": self.region}
            )

        def fetch():
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                return json.loads(response.read(8192))["url"]

        return await asyncio.to_thread(fetch)

    async def close(self):
        """Close the active WebSocket connection, if any."""
        if self._ws:
            await self._ws.close()
        self._ws = None

    async def _request(self, op, **parameters):
        self._request_id += 1
        request_id = self._request_id
        await self._ws.send(
            json.dumps({"op": op, "request_id": request_id, **parameters})
        )
        while True:
            event = json.loads(
                await asyncio.wait_for(self._ws.recv(), self.timeout)
            )
            if event.get("type") == "closed":
                raise ConnectionError("session closed; rediscover")
            if (
                event.get("type") == "response"
                and event.get("request_id") == request_id
            ):
                if not event["ok"]:
                    if "service unavailable" in event["error"]:
                        raise ConnectionError("chat service unavailable")
                    raise chat_system.models.ChatError(event["error"])
                return event["result"]
            # Live events are hints. sync remains authoritative and cannot skip
            # earlier IDs.

    async def call(self, op, **parameters):
        """Send an operation, retrying transient connection failures.

        Args:
            op: Chat protocol operation name.
            **parameters: Operation fields encoded in the JSON request.

        Returns:
            The result field of the matching successful response.

        Raises:
            ChatError: The server rejects the operation.
            OSError: Reconnection attempts are exhausted by an I/O failure.
        """
        if websockets is None:
            raise ImportError("Install the websocket extra")

        async with self._lock:
            for attempt in range(self.max_reconnects):
                try:
                    if self._ws is None:
                        self.endpoint = await self.discover()
                        self._ws = await websockets.asyncio.client.connect(
                            self.endpoint,
                            open_timeout=self.timeout,
                            max_size=128_000_000,
                        )
                        await self._request(
                            "login", token=self.token, device_id=self.device_id
                        )
                    return await self._request(op, **parameters)
                except (
                    OSError,
                    websockets.exceptions.ConnectionClosed,
                    asyncio.TimeoutError,
                    ConnectionError,
                ):
                    await self.close()
                    if attempt + 1 == self.max_reconnects:
                        raise
                    await asyncio.sleep(self.retry_delay)
                except chat_system.models.ChatError:
                    await self.close()
                    raise

    async def receive(self, limit=100):
        """Return up to limit inbox messages after the last acknowledged
        cursor.
        """
        result = await self.call("sync", after=self.cursor, limit=limit)
        return result["messages"]

    def acknowledge(self, messages):
        """Call only after processing a complete receive() page
        successfully.
        """
        if not messages:
            return
        new_cursor = max(self.cursor, int(messages[-1]["message_id"]))
        if self.cursor_file:
            temporary = self.cursor_file.with_name(
                self.cursor_file.name + ".tmp"
            )
            with temporary.open("w", encoding="utf-8") as file:
                file.write(str(new_cursor))
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(self.cursor_file)
        self.cursor = new_cursor

    async def watch(self, interval=1):
        """Yield ordered sync pages; caller explicitly acknowledges each
        processed page.
        """
        while True:
            await self.call("heartbeat")
            page = await self.receive()
            if page:
                yield page
            else:
                await asyncio.sleep(interval)
