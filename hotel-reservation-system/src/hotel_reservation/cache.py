"""Isolated TTL LRU values and bounded single-flight cold loads."""

from collections import OrderedDict
import json
import math
import threading
import time


class Cache:
    def __init__(self, ttl=1, limit=128, clock=time.monotonic):
        if type(ttl) not in (int, float) or not 0 <= ttl <= 3600:
            raise ValueError("cache ttl must be 0..3600 seconds")
        if type(limit) is not int or not 0 <= limit <= 10000 or not callable(clock):
            raise ValueError("invalid cache configuration")
        self.ttl, self.limit, self.clock = ttl, limit, clock
        self.values, self.lock = OrderedDict(), threading.Lock()
        self._loads = tuple(threading.RLock() for _ in range(64))

    def _now(self):
        value = self.clock()
        if (
            type(value) not in (int, float)
            or not 0 <= value <= 2**53
            or not math.isfinite(value)
        ):
            raise ValueError("invalid cache clock")
        return value

    def get(self, key):
        with self.lock:
            pair = self.values.get(key)
            if pair is None or pair[0] <= self._now():
                self.values.pop(key, None)
                return None
            self.values.move_to_end(key)
            payload = pair[1]
        return json.loads(payload)

    def put(self, key, value):
        payload = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        with self.lock:
            self.values[key] = (self._now() + self.ttl, payload)
            self.values.move_to_end(key)
            while len(self.values) > self.limit:
                self.values.popitem(last=False)

    def load(self, key, loader):
        result = self.get(key)
        if result is not None:
            return result
        with self._loads[hash(key) % len(self._loads)]:
            result = self.get(key)
            if result is not None:
                return result
            result = loader()
            self.put(key, result)
            # Even the loading caller gets an independent decoded object.
            return json.loads(json.dumps(result, allow_nan=False))
