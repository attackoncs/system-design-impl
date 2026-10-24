"""Bounded DNS cache for crawler socket connections.

Resolution changes only the socket destination. The HTTP connection still owns
the original host name, which is needed for Host headers and TLS verification.
"""

from __future__ import annotations

import collections
import math
import socket
import threading
import time
from typing import Callable, Optional


class DNSCache:
    """Thread-safe, bounded LRU cache with lazy TTL refresh and lookup coalescing.

    ``getaddrinfo`` does not expose authoritative DNS TTLs. ``ttl`` is therefore
    an application refresh interval, not the DNS record's advertised lifetime.
    Failed lookups are not cached.
    """

    def __init__(self, ttl: float = 300.0, capacity: int = 1024) -> None:
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
            raise ValueError("DNS ttl must be a finite positive number")
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("DNS ttl must be a finite positive number")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("DNS capacity must be a positive integer")
        self._ttl = float(ttl)
        self._capacity = capacity
        self._entries: collections.OrderedDict[tuple, tuple] = collections.OrderedDict()
        self._lock = threading.Lock()
        self._lookup_locks = [threading.Lock() for _ in range(64)]

    def resolve(self, host: str, port: int) -> tuple:
        """Return immutable stream socket addresses, sharing concurrent misses."""
        key = (host.lower(), port)
        with self._lookup_locks[hash(key) % len(self._lookup_locks)]:
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None and entry[0] > time.monotonic():
                    self._entries.move_to_end(key)
                    return entry[1]
                self._entries.pop(key, None)
            addresses = tuple(socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM))
            if not addresses:
                raise OSError("DNS lookup returned no socket addresses")
            with self._lock:
                self._entries[key] = (time.monotonic() + self._ttl, addresses)
                while len(self._entries) > self._capacity:
                    self._entries.popitem(last=False)
            return addresses

    def create_connection(
        self,
        address: tuple[str, int],
        timeout: float,
        source_address: Optional[tuple] = None,
        *,
        remaining: Optional[Callable[[], float]] = None,
        register_socket: Optional[Callable[[socket.socket], None]] = None,
    ) -> socket.socket:
        """Connect to a resolved address while retaining one overall deadline."""
        addresses = self.resolve(*address)
        last_error: Optional[OSError] = None
        for family, kind, proto, _, destination in addresses:
            sock = socket.socket(family, kind, proto)
            try:
                sock.settimeout(remaining() if remaining is not None else timeout)
                if source_address is not None:
                    sock.bind(source_address)
                if register_socket is not None:
                    register_socket(sock)
                sock.connect(destination)
                return sock
            except OSError as error:
                last_error = error
                sock.close()
        if last_error is None:
            raise OSError("No connection addresses available")
        raise last_error
