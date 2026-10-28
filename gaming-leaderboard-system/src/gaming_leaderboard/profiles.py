"""Bounded profile cache tied to the authoritative display-name revision."""

from collections import OrderedDict
import threading


class ProfileCache:
    def __init__(self, capacity=1024, byte_limit=1024 * 1024):
        self.capacity, self.byte_limit = capacity, byte_limit
        self.lock = threading.Lock()
        self.values = OrderedDict()
        self.bytes = self.hits = self.misses = 0

    def names(self, journal, revision, users):
        names, missing = {}, []
        with self.lock:
            for user in dict.fromkeys(users):
                key = (revision, user)
                if key in self.values:
                    self.hits += 1
                    names[user] = self.values[key][0]
                    self.values.move_to_end(key)
                else:
                    self.misses += 1
                    missing.append(user)
        loaded = journal.names(missing) if missing else {}
        with self.lock:
            for user, name in loaded.items():
                key, size = (
                    (revision, user),
                    len(user.encode()) + len(name.encode()) + 16,
                )
                if self.capacity and size <= self.byte_limit:
                    old = self.values.pop(key, None)
                    if old:
                        self.bytes -= old[1]
                    self.values[key] = (name, size)
                    self.bytes += size
                while self.values and (
                    len(self.values) > self.capacity or self.bytes > self.byte_limit
                ):
                    _, (_, removed) = self.values.popitem(last=False)
                    self.bytes -= removed
        return {**names, **loaded}

    def stats(self):
        with self.lock:
            return {
                "entries": len(self.values),
                "bytes": self.bytes,
                "hits": self.hits,
                "misses": self.misses,
            }
