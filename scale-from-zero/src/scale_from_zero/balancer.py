"""Round-robin routing and explicit replay policy, independent of HTTP parsing."""

import threading
from urllib.parse import urlsplit

from .common import Error, request


class LoadBalancer:
    def __init__(self, endpoints, transport=request):
        if not endpoints:
            raise ValueError("at least one web endpoint required")
        self._endpoints = tuple(endpoints)
        self._transport = transport
        self._lock = threading.Lock()
        self._next = 0
        self._failovers = 0

    @property
    def failovers(self):
        with self._lock:
            return self._failovers

    def forward(self, method, path, token, body=None):
        # Only profile writes implement request-id deduplication. A field named
        # request_id does not make login or future endpoints safe to replay.
        safe = method == "GET" or (
            method == "POST"
            and urlsplit(path).path == "/v1/profile"
            and isinstance(body, dict)
            and isinstance(body.get("request_id"), str)
            and bool(body["request_id"])
        )
        with self._lock:
            start = self._next
            self._next = (start + 1) % len(self._endpoints)
        last = None
        for offset in range(len(self._endpoints) if safe else 1):
            endpoint = self._endpoints[(start + offset) % len(self._endpoints)]
            try:
                return self._transport(endpoint + path, token, body, timeout=1.5)
            except Error as exc:
                if exc.status < 500:
                    raise
                last = exc
            except (OSError, TimeoutError) as exc:
                last = exc
            with self._lock:
                self._failovers += 1
        raise Error(503, "no eligible web node available: " + type(last).__name__)
