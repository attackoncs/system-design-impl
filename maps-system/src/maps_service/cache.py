"""Entry- and byte-bounded LRU cache with isolated values and coalesced cold loads.

Each entry is sized once, when it is inserted, with ``estimate_bytes``. The
estimate counts payload bytes (encoded tile bytes, cached JSON text) plus a
small fixed charge per container slot and scalar. It is deterministic and
cheap, but it is a budget for cached payload, not a measurement of process RSS
(Python object headers and allocator slack are not counted).
"""

from collections import OrderedDict
from copy import deepcopy
import threading

_MISSING = object()
_SLOT_BYTES = 8  # fixed charge per scalar or container slot

DEFAULT_MAX_BYTES = 64 * 1024 * 1024


def estimate_bytes(value):
    """Deterministic payload-size estimate used for the cache byte budget."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(
            _SLOT_BYTES + estimate_bytes(k) + estimate_bytes(v)
            for k, v in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return sum(_SLOT_BYTES + estimate_bytes(item) for item in value)
    return _SLOT_BYTES


class Cache:
    def __init__(self, capacity=128, max_bytes=DEFAULT_MAX_BYTES):
        if type(capacity) is not int or capacity < 0:
            raise ValueError("capacity must be a non-negative integer")
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")
        self.capacity, self.max_bytes = capacity, max_bytes
        self.items = OrderedDict()
        self.sizes = {}
        self.bytes = 0
        self.lock = threading.Lock()
        self.hits = self.misses = self.evictions = self.oversized = 0
        self._loads = tuple(threading.RLock() for _ in range(64))

    def _lookup(self, key):
        with self.lock:
            if key not in self.items:
                return _MISSING
            self.hits += 1
            self.items.move_to_end(key)
            return self.items[key]

    def _store(self, key, value, size):
        """Insert under ``self.lock``; evict LRU entries until both bounds hold."""
        if not self.capacity or size > self.max_bytes:
            if self.capacity and self.max_bytes:
                self.oversized += 1  # served to the caller, never cached
            return
        self.items[key] = value
        self.sizes[key] = size
        self.bytes += size
        while len(self.items) > self.capacity or self.bytes > self.max_bytes:
            old, _ = self.items.popitem(last=False)
            self.bytes -= self.sizes.pop(old)
            self.evictions += 1

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
                size = estimate_bytes(value)
                with self.lock:
                    self._store(key, value, size)
            return value

    def get(self, key, loader):
        return deepcopy(self._get(key, loader))

    def get_item(self, key, item, loader):
        """Copy only one adjacency list, not its entire cached source tile."""
        return deepcopy(self._get(key, loader).get(item, []))

    def stats(self):
        with self.lock:
            return {
                "entries": len(self.items),
                "bytes": self.bytes,
                "max_entries": self.capacity,
                "max_bytes": self.max_bytes,
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
                "oversized": self.oversized,
            }
