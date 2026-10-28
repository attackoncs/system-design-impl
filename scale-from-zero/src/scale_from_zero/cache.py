"""Byte/entry-bounded LRU, version floors and generation-fenced cache fills."""

from collections import OrderedDict
import copy
import secrets
import threading
import time

from .common import Error, encode, integer
from .contracts import freeze, timestamp
from .transport import seconds


class Cache:
    def __init__(self, capacity=32, ttl=2, clock=time.monotonic, max_bytes=512 * 1024):
        self.capacity = integer(capacity, 1, 1000)
        self.ttl, self.clock = seconds(ttl), clock
        self.max_bytes = integer(max_bytes, 1, 8 * 1024 * 1024)
        self.values, self.floors = OrderedDict(), OrderedDict()
        self.hits = self.misses = self.evictions = self.rejected = self._bytes = 0
        self._lock = threading.Lock()
        self._instance, self._generation = secrets.token_hex(16), 0

    def _ticket(self):
        return self._instance + ":" + str(self._generation)

    def _remove(self, key):
        value = self.values.pop(key, None)
        if value:
            self._bytes -= value[2]

    def probe(self, key, min_version=0):
        integer(min_version)
        with self._lock:
            now = timestamp(self.clock())
            floor = max(min_version, self.floors.get(key, 0))
            old = self.values.get(key)
            ticket = self._ticket()
            if old and old[0] > now and old[1].get("version", 0) >= floor:
                self.values.move_to_end(key)
                self.hits += 1
                value = old[1]
            else:
                if old and old[0] <= now:
                    self._remove(key)
                self.misses += 1
                value = None
        return {
            "value": copy.deepcopy(value),
            "generation": ticket,
            "min_version": floor,
        }

    def get(self, key, min_version=0):
        return self.probe(key, min_version)["value"]

    def put(self, key, value, generation=None):
        snapshot = freeze(value)
        if not isinstance(snapshot, dict):
            raise ValueError("cache object required")
        integer(snapshot.get("version", 0))
        size = len(encode(snapshot).encode())
        with self._lock:
            now = timestamp(self.clock())
            old = self.values.get(key)
            floor = self.floors.get(key, 0)
            if (
                generation is not None
                and generation != self._ticket()
                or snapshot.get("version", 0) < floor
                or size > self.max_bytes
            ):
                self.rejected += 1
                return False
            if old and old[0] > now:
                if snapshot.get("version", 0) < old[1].get("version", 0):
                    self.rejected += 1
                    return False
                if (
                    snapshot.get("version", 0) == old[1].get("version", 0)
                    and snapshot != old[1]
                ):
                    raise Error(409, "same-version cache content conflict")
            self._remove(key)
            for expired in [name for name, row in self.values.items() if row[0] <= now]:
                self._remove(expired)
            self.values[key] = now + self.ttl, snapshot, size
            self._bytes += size
            self.values.move_to_end(key)
            while len(self.values) > self.capacity or self._bytes > self.max_bytes:
                self._remove(next(iter(self.values)))
                self.evictions += 1
            return True

    def delete(self, key, min_version=0):
        integer(min_version)
        with self._lock:
            self._remove(key)
            self._generation += 1
            if min_version:
                self.floors[key] = max(min_version, self.floors.get(key, 0))
                self.floors.move_to_end(key)
                if len(self.floors) > 1000:
                    self.floors.popitem(last=False)

    def status(self):
        with self._lock:
            return {
                "entries": len(self.values),
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
                "capacity": self.capacity,
                "ttl_seconds": self.ttl,
                "bytes": self._bytes,
                "max_bytes": self.max_bytes,
                "version_floors": len(self.floors),
                "rejected_fills": self.rejected,
            }
