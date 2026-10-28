"""Bounded TTL cache with isolated values and coalesced loads outside the LRU lock."""

from collections import OrderedDict
from copy import deepcopy
import math
import threading
import time

_MISSING = object()


class Cache:
    """Cache public JSON-like records or immutable ID tuples.

    Cache hits and successful loads return isolated values. A fixed set of stripes
    bounds synchronization state. Warm entries never wait for a cold loader.
    Misses count actual loader invocations, including failed invocations.
    """

    def __init__(self, ttl=60, capacity=2048, clock=time.monotonic):
        try:
            valid = type(ttl) in (int, float) and math.isfinite(ttl) and ttl > 0
        except OverflowError:
            valid = False
        if (
            not valid
            or type(capacity) is not int
            or capacity < 0
            or not callable(clock)
        ):
            raise ValueError("invalid cache configuration")
        self.ttl, self.capacity, self.clock = ttl, capacity, clock
        self.items = OrderedDict()
        self.hits = self.misses = 0
        self.lock = threading.Lock()
        self._loads = tuple(threading.RLock() for _ in range(64))

    def _lookup(self, key):
        with self.lock:
            entry = self.items.get(key)
            if entry is not None:
                if entry[0] > self.clock():
                    self.hits += 1
                    self.items.move_to_end(key)
                    return entry[1]
                del self.items[key]
            return _MISSING

    def get(self, key, loader):
        value = self._lookup(key)
        if value is not _MISSING:
            return deepcopy(value)
        with self._loads[hash(key) % len(self._loads)]:
            value = self._lookup(key)
            if value is _MISSING:
                with self.lock:
                    self.misses += 1
                value = deepcopy(loader())
                # TTL starts at successful completion, not before slow I/O.
                expires = self.clock() + self.ttl
                with self.lock:
                    if self.capacity:
                        self.items[key] = (expires, value)
                        self.items.move_to_end(key)
                        while len(self.items) > self.capacity:
                            self.items.popitem(last=False)
            return deepcopy(value)
