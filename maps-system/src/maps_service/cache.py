"""Entry-bounded LRU cache with isolated values and coalesced cold loads."""

from collections import OrderedDict
from copy import deepcopy
import threading

_MISSING = object()


class Cache:
    def __init__(self, capacity=128):
        if type(capacity) is not int or capacity < 0:
            raise ValueError("capacity must be a non-negative integer")
        self.capacity, self.items = capacity, OrderedDict()
        self.lock = threading.Lock()
        self.hits = self.misses = 0
        self._loads = tuple(threading.RLock() for _ in range(64))

    def _lookup(self, key):
        with self.lock:
            if key not in self.items:
                return _MISSING
            self.hits += 1
            self.items.move_to_end(key)
            return self.items[key]

    def _get(self, key, loader):
        value = self._lookup(key)
        if value is not _MISSING:
            return value
        with self._loads[hash(key) % len(self._loads)]:
            value = self._lookup(key)
            if value is _MISSING:
                with self.lock:
                    self.misses += 1
                value = deepcopy(loader())
                with self.lock:
                    if self.capacity:
                        self.items[key] = value
                        self.items.move_to_end(key)
                        while len(self.items) > self.capacity:
                            self.items.popitem(last=False)
            return value

    def get(self, key, loader):
        return deepcopy(self._get(key, loader))

    def get_item(self, key, item, loader):
        """Copy only one adjacency list, not its entire cached source tile."""
        return deepcopy(self._get(key, loader).get(item, []))
