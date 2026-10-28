"""Round-robin routing and explicit replay policy, independent of HTTP parsing."""

import threading
from urllib.parse import urlsplit

from .common import Error, request
from .contracts import freeze


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

    def configure(self, endpoints):
        endpoints = tuple(endpoints)
        if not 1 <= len(endpoints) <= 8 or len(set(endpoints)) != len(endpoints):
            raise ValueError("1..8 distinct web endpoints required")
        with self._lock:
            self._endpoints, self._next = endpoints, 0
        return self.status()

    def status(self):
        with self._lock:
            return {"endpoints": list(self._endpoints), "failovers": self._failovers}

    def forward(self, method, path, token, body=None, *, deadline=None, cancel=None):
        body = freeze(body)
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
            endpoints = self._endpoints
            start = self._next
            self._next = (start + 1) % len(endpoints)
        last = None
        for offset in range(len(endpoints) if safe else 1):
            endpoint = endpoints[(start + offset) % len(endpoints)]
            try:
                return self._transport(
                    endpoint + path,
                    token,
                    body,
                    timeout=1.5,
                    deadline=deadline,
                    cancel=cancel,
                )
            except Error as exc:
                if exc.status < 500:
                    raise
                last = exc
            except (OSError, TimeoutError) as exc:
                last = exc
            with self._lock:
                self._failovers += 1
        raise Error(503, "no eligible web node available: " + type(last).__name__)
