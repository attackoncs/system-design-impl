"""Byte/entry-bounded cache, version floors and generation-fenced fills.

The default policy is least-recently-used. Least-frequently-used and first-in
first-out are available for the chapter's other eviction choices. set/get
follow the chapter's cache API: set stores a value for an explicit TTL.
"""

from collections import OrderedDict
import copy
import secrets
import threading
import time

from .common import Error, encode, integer
from .contracts import freeze, timestamp
from .transport import seconds


class Cache:
    def __init__(
        self,
        capacity=32,
        ttl=2,
        clock=time.monotonic,
        max_bytes=512 * 1024,
        policy="lru",
    ):
        if policy not in {"lru", "lfu", "fifo"}:
            raise ValueError("cache policy must be lru, lfu, or fifo")
        self.capacity = integer(capacity, 1, 1000)
        self.ttl, self.clock = seconds(ttl), clock
        self.max_bytes = integer(max_bytes, 1, 8 * 1024 * 1024)
        self.policy = policy
        self.values, self.floors = OrderedDict(), OrderedDict()
        self._freq = {}
        self.hits = self.misses = self.evictions = self.rejected = self._bytes = 0
        self._lock = threading.Lock()
        self._instance, self._generation = secrets.token_hex(16), 0

    def _ticket(self):
        return self._instance + ":" + str(self._generation)

    def _remove(self, key):
        value = self.values.pop(key, None)
        self._freq.pop(key, None)
        if value:
            self._bytes -= value[2]

    def _victim(self):
        if self.policy != "lfu":
            return next(iter(self.values))
        least = min(self._freq[key] for key in self.values)
        for key in self.values:
            if self._freq[key] == least:
                return key
        return next(iter(self.values))

    def probe(self, key, min_version=0):
        integer(min_version)
        with self._lock:
            now = timestamp(self.clock())
            floor = max(min_version, self.floors.get(key, 0))
            old = self.values.get(key)
            ticket = self._ticket()
            if old and old[0] > now and old[1].get("version", 0) >= floor:
                if self.policy == "lru":
                    self.values.move_to_end(key)
                elif self.policy == "lfu":
                    self._freq[key] = self._freq.get(key, 0) + 1
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
        value = self.probe(key, min_version)["value"]
        if (
            isinstance(value, dict)
            and set(value) == {"memo", "version"}
            and value["version"] == 0
            and isinstance(value["memo"], str)
        ):
            return value["memo"]
        return value

    def set(self, key, value, ttl):
        """Store a chapter-style text value, or a profile object, for ttl seconds."""
        if isinstance(value, str):
            if not value.isascii() or not 1 <= len(value) <= 256:
                raise ValueError("cache text must be 1..256 ASCII characters")
            value = {"version": 0, "memo": value}
        return self.put(key, value, ttl=ttl)

    def put(self, key, value, generation=None, ttl=None):
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
            seen = self._freq.get(key, 0)
            self._remove(key)
            for expired in [name for name, row in self.values.items() if row[0] <= now]:
                self._remove(expired)
            lifetime = self.ttl if ttl is None else seconds(ttl)
            self.values[key] = now + lifetime, snapshot, size
            self._bytes += size
            if self.policy == "lfu":
                self._freq[key] = seen + 1
            self.values.move_to_end(key)
            while len(self.values) > self.capacity or self._bytes > self.max_bytes:
                self._remove(self._victim())
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
                "policy": self.policy,
            }


class CacheCluster:
    """Read and write several caches so one dead member is not a single point of failure.

    A read returns the first fresh hit. A write or delete is attempted on every
    member and does not hold the cluster lock across that work. Generation
    tickets stay local to the member that issued them.
    """

    def __init__(self, members):
        if not isinstance(members, (list, tuple)) or not 1 <= len(members) <= 8:
            raise ValueError("1..8 cache members required")
        self._members = tuple(members)
        self._lock = threading.Lock()
        self.errors = 0

    def get(self, key, min_version=0):
        for cache in self._members:
            try:
                value = cache.get(key, min_version)
            except Exception:
                with self._lock:
                    self.errors += 1
                continue
            if value is not None:
                return value
        return None

    def put(self, key, value, generation=None):
        stored = False
        for cache in self._members:
            try:
                stored = bool(cache.put(key, value, generation)) or stored
            except Exception:
                with self._lock:
                    self.errors += 1
        return stored

    def delete(self, key, min_version=0):
        for cache in self._members:
            try:
                cache.delete(key, min_version)
            except Exception:
                with self._lock:
                    self.errors += 1

    def set(self, key, value, ttl):
        stored = False
        for cache in self._members:
            try:
                stored = bool(cache.set(key, value, ttl)) or stored
            except Exception:
                with self._lock:
                    self.errors += 1
        return stored
