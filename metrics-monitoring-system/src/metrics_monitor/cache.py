"""Small owned-result LRU with bounded cold-load coordination."""

from collections import OrderedDict
from copy import deepcopy
import threading


class Cache:
    def __init__(self, capacity=128):
        if type(capacity) is not int or not 0 <= capacity <= 10000:
            raise ValueError("capacity must be an integer in 0..10000")
        self.capacity = capacity
        self.items = OrderedDict()
        self.lock = threading.Lock()
        self._loads = tuple(threading.RLock() for _ in range(64))
        self.hits = self.misses = 0

    def get(self, key, loader):
        # Bounded stripes coalesce identical misses; collisions only serialize
        # cold loads. Neither loading nor copying holds the global metadata lock.
        with self.lock:
            if key in self.items:
                self.hits += 1
                self.items.move_to_end(key)
                found = self.items[key]
            else:
                self.misses += 1
                found = None
            present = key in self.items
        if present:
            return deepcopy(found)
        with self._loads[hash(key) % len(self._loads)]:
            with self.lock:
                present = key in self.items
                if present:
                    self.items.move_to_end(key)
                    found = self.items[key]
            if not present:
                found = deepcopy(loader())
                with self.lock:
                    if self.capacity:
                        self.items[key] = found
                        while len(self.items) > self.capacity:
                            self.items.popitem(last=False)
        return deepcopy(found)

    def clear(self):
        with self.lock:
            self.items.clear()
