"""Capacity-aware reference service registry (not a distributed coordinator)."""
from dataclasses import dataclass

from .models import ChatError


@dataclass(frozen=True)
class Server:
    server_id: str
    url: str
    region: str
    capacity: int
    connections: int = 0


class ServiceDiscovery:
    def __init__(self):
        self._servers = {}

    def register(self, server):
        if server.capacity <= 0 or not 0 <= server.connections <= server.capacity:
            raise ChatError("invalid capacity")
        self._servers[server.server_id] = server

    def unregister(self, server_id):
        self._servers.pop(server_id, None)

    def select(self, region=None):
        available = [s for s in self._servers.values() if s.connections < s.capacity]
        if not available:
            raise ChatError("no chat server available")
        return min(available, key=lambda s: (region is not None and s.region != region,
                                            s.connections / s.capacity, s.server_id))
