"""Capacity-aware reference service registry (not a distributed
coordinator).
"""

import dataclasses
import threading
from urllib.parse import urlsplit

import chat_system.models


@dataclasses.dataclass(frozen=True)
class Server:
    """A registry entry with endpoint, region, capacity, and connection
    count.
    """

    server_id: str
    url: str
    region: str
    capacity: int
    connections: int = 0


class ServiceDiscovery:
    """An in-process registry selecting servers by region and available
    capacity.
    """

    def __init__(self):
        self._servers = {}
        self._lock = threading.RLock()

    def register(self, server):
        """Validate capacity and insert or replace a Server by its
        server_id.
        """
        try:
            chat_system.models.require_integer(server.capacity, "capacity")
            chat_system.models.require_integer(
                server.connections, "connections", 0, server.capacity
            )
            parsed = urlsplit(server.url)
            if (
                parsed.scheme not in {"ws", "wss"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("invalid endpoint")
            if (
                not isinstance(server.server_id, str)
                or not server.server_id.strip()
                or not isinstance(server.region, str)
            ):
                raise ValueError("invalid identity")
            # Accessing port validates malformed and out-of-range values.
            parsed.port
        except (AttributeError, TypeError, ValueError) as error:
            raise chat_system.models.ChatError("invalid server registration") from error
        with self._lock:
            self._servers[server.server_id] = server

    def unregister(self, server_id):
        """Remove server_id from discovery, ignoring absent registrations."""
        with self._lock:
            self._servers.pop(server_id, None)

    def select(self, region=None):
        """Return the least-loaded available server, preferring the requested
        region.

        Args:
            region: Preferred region, or None to rank all regions equally.

        Returns:
            The available Server with the lowest fractional connection load.

        Raises:
            ChatError: No registered server has spare capacity.
        """
        with self._lock:
            available = [
                s for s in self._servers.values() if s.connections < s.capacity
            ]
        if not available:
            raise chat_system.models.ChatError("no chat server available")
        return min(
            available,
            key=lambda s: (
                region is not None and s.region != region,
                s.connections / s.capacity,
                s.server_id,
            ),
        )
