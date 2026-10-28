"""Process-local bounded owner buckets; internal callbacks retain HTTP capacity."""

import threading
import time

from .common import Error


class OwnerRate:
    def __init__(self, owners, clock=time.monotonic, rate=100, burst=100):
        self.clock, self.rate, self.burst = clock, rate, burst
        self.values = {owner: (burst, clock()) for owner in owners}
        self.lock = threading.Lock()

    def take(self, owner):
        with self.lock:
            remaining, previous = self.values[owner]
            now = self.clock()
            remaining = min(self.burst, remaining + max(0, now - previous) * self.rate)
            if remaining < 1:
                raise Error(429, "owner submission rate exceeded")
            self.values[owner] = remaining - 1, max(now, previous)
