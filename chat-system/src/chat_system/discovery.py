"""Capacity-aware reference service registry (not a distributed
coordinator).
"""

import dataclasses

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

    def register(self, server):
        """Validate capacity and insert or replace a Server by its
        server_id.
        """
        if (
            server.capacity <= 0
            or not 0 <= server.connections <= server.capacity
        ):
            raise chat_system.models.ChatError("invalid capacity")
        self._servers[server.server_id] = server

    def unregister(self, server_id):
        """Remove server_id from discovery, ignoring absent registrations."""
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
