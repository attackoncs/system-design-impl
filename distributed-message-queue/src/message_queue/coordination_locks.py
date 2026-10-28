"""Bounded, ordered operation locks for a single metadata owner.

SQLite transactions protect metadata visibility; these locks protect the longer
read/replicate/publish protocol while allowing unrelated partitions to progress.
They do not coordinate multiple coordinator processes against one database.
"""

from contextlib import contextmanager
import hashlib
import threading

from .common import dumps


class OperationLocks:
    def __init__(self, stripes=128):
        self._locks = tuple(threading.RLock() for _ in range(stripes))

    def index(self, key):
        digest = hashlib.sha256(dumps(key).encode()).digest()
        return int.from_bytes(digest[:8], "big") % len(self._locks)

    @contextmanager
    def hold(self, *keys):
        locks = [self._locks[i] for i in sorted({self.index(key) for key in keys})]
        acquired = []
        try:
            for lock in locks:
                lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()


def partition_key(topic, partition):
    return ("partition", topic, partition)
