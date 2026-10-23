"""Bounded, thread-safe LRU cache with monotonic expiry and copy isolation."""

from collections import OrderedDict
import copy
import threading
import time

from .common import integer


class Cache:
    def __init__(self, capacity=32, ttl=2, clock=time.monotonic):
        self.capacity = integer(capacity, 1, 1000)
        if isinstance(ttl, bool) or not 0 < ttl <= 3600:
            raise ValueError("TTL must be 0..3600 seconds")
        self.ttl, self.clock = ttl, clock
        self.values = OrderedDict()
        self.hits = self.misses = self.evictions = 0
        self._lock = threading.Lock()

    def get(self, key, min_version=0):
        integer(min_version)
        with self._lock:
            old = self.values.get(key)
            now = self.clock()
            if old and old[0] > now and old[1].get("version", 0) >= min_version:
                self.values.move_to_end(key)
                self.hits += 1
                value = old[1]
            else:
                if old and old[0] <= now:
                    self.values.pop(key)
                self.misses += 1
                return None
        # Stored snapshots are never mutated internally. Copy outside the lock.
        return copy.deepcopy(value)

    def put(self, key, value):
        snapshot = copy.deepcopy(value)
        with self._lock:
            self.values[key] = (self.clock() + self.ttl, snapshot)
            self.values.move_to_end(key)
            if len(self.values) > self.capacity:
                self.values.popitem(last=False)
                self.evictions += 1

    def delete(self, key):
        with self._lock:
            self.values.pop(key, None)

    def status(self):
        with self._lock:
            return {
                "entries": len(self.values),
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
                "capacity": self.capacity,
                "ttl_seconds": self.ttl,
            }
