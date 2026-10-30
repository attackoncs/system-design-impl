"""Local geo-routing model for the chapter's two-datacenter step.

Users are mapped to the nearest configured data center. When that center is
unhealthy, every lookup moves to the next healthy center, which is the
chapter's "100% of traffic to the surviving data center" failover. This is
not a DNS server, GeoDNS provider, or cross-region disk replica.

Profile copies between centers reuse the existing pull in workers.replicate_once:
point the remote replica at the home primary. The directory only chooses which
entry a client should call.
"""

import hashlib
import threading

from .common import Error, identifier


class GeoDNS:
    def __init__(self, centers, locations):
        if (
            not isinstance(centers, dict)
            or not 2 <= len(centers) <= 4
            or not isinstance(locations, dict)
        ):
            raise ValueError("2..4 data centers and a location map required")
        self._centers = {}
        for name, entry in centers.items():
            identifier(name)
            if not isinstance(entry, str) or not 1 <= len(entry) <= 200:
                raise ValueError("data center entry must be a short label")
            self._centers[name] = entry
        if len(self._centers) != len(centers):
            raise ValueError("data center names must be unique")
        self._order = tuple(centers)
        self._locations = {}
        for location, name in locations.items():
            identifier(location)
            if name not in self._centers:
                raise ValueError("location points at an unknown data center")
            self._locations[location] = name
        self._healthy = {name: True for name in self._order}
        self._lock = threading.Lock()

    def set_health(self, name, healthy):
        identifier(name)
        if name not in self._centers or type(healthy) is not bool:
            raise ValueError("known data center and boolean health required")
        with self._lock:
            self._healthy[name] = healthy

    def resolve(self, location):
        """Return the nearest healthy center, or the next healthy one if it is down."""
        identifier(location)
        with self._lock:
            healthy = {name for name in self._order if self._healthy[name]}
            if not healthy:
                raise Error(503, "no healthy data center")
            preferred = self._locations.get(location)
            if preferred is None:
                preferred = self._order[
                    int(hashlib.sha256(location.encode()).hexdigest(), 16)
                    % len(self._order)
                ]
            start = self._order.index(preferred)
            for offset in range(len(self._order)):
                name = self._order[(start + offset) % len(self._order)]
                if name in healthy:
                    return {"datacenter": name, "entry": self._centers[name]}
        raise Error(503, "no healthy data center")
