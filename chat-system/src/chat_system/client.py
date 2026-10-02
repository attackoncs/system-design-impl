"""Discover/reconnect client with explicit durable inbox acknowledgement."""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from .models import ChatError


class ReconnectingChatClient:
    def __init__(self, discovery_url, token, device_id, region=None, cursor_file=None,
                 timeout=5, retry_delay=0.2, max_reconnects=30):
        if timeout <= 0 or retry_delay < 0 or max_reconnects < 1:
            raise ValueError("invalid reconnect settings")
        self.discovery_url, self.token, self.device_id, self.region = discovery_url, token, device_id, region
        self.cursor_file = Path(cursor_file) if cursor_file else None
        self.cursor = 0
        if self.cursor_file and self.cursor_file.exists():
            self.cursor = int(self.cursor_file.read_text())
            if self.cursor < 0:
                raise ValueError("invalid saved cursor")
        self.timeout, self.retry_delay, self.max_reconnects = timeout, retry_delay, max_reconnects
        self._ws = None
        self._lock = asyncio.Lock()
        self._request_id = 0
        self.endpoint = None

    async def discover(self):
        url = self.discovery_url
        if self.region:
            url += ("&" if "?" in url else "?") + urlencode({"region": self.region})

        def fetch():
            with urlopen(url, timeout=self.timeout) as response:
                return json.loads(response.read(8192))["url"]

        return await asyncio.to_thread(fetch)

    async def close(self):
        if self._ws:
            await self._ws.close()
        self._ws = None

    async def _request(self, op, **parameters):
        self._request_id += 1
        request_id = self._request_id
        await self._ws.send(json.dumps({"op": op, "request_id": request_id, **parameters}))
        while True:
            event = json.loads(await asyncio.wait_for(self._ws.recv(), self.timeout))
            if event.get("type") == "closed":
                raise ConnectionError("session closed; rediscover")
            if event.get("type") == "response" and event.get("request_id") == request_id:
                if not event["ok"]:
                    if "service unavailable" in event["error"]:
                        raise ConnectionError("chat service unavailable")
                    raise ChatError(event["error"])
                return event["result"]
            # Live events are hints. sync remains authoritative and cannot skip earlier IDs.

    async def call(self, op, **parameters):
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed

        async with self._lock:
            for attempt in range(self.max_reconnects):
                try:
                    if self._ws is None:
                        self.endpoint = await self.discover()
                        self._ws = await connect(self.endpoint, open_timeout=self.timeout, max_size=128_000_000)
                        await self._request("login", token=self.token, device_id=self.device_id)
                    return await self._request(op, **parameters)
                except (OSError, ConnectionClosed, asyncio.TimeoutError, ConnectionError):
                    await self.close()
                    if attempt + 1 == self.max_reconnects:
                        raise
                    await asyncio.sleep(self.retry_delay)
                except ChatError:
                    await self.close()
                    raise

    async def receive(self, limit=100):
        result = await self.call("sync", after=self.cursor, limit=limit)
        return result["messages"]

    def acknowledge(self, messages):
        """Call only after processing a complete receive() page successfully."""
        if not messages:
            return
        new_cursor = max(self.cursor, int(messages[-1]["message_id"]))
        if self.cursor_file:
            temporary = self.cursor_file.with_name(self.cursor_file.name + ".tmp")
            with temporary.open("w", encoding="utf-8") as file:
                file.write(str(new_cursor))
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(self.cursor_file)
        self.cursor = new_cursor

    async def watch(self, interval=1):
        """Yield ordered sync pages; caller explicitly acknowledges each processed page."""
        while True:
            await self.call("heartbeat")
            page = await self.receive()
            if page:
                yield page
            else:
                await asyncio.sleep(interval)
