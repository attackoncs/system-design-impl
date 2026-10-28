"""Sample one validated clock after acquiring each SQLite writer transaction."""

from contextlib import contextmanager
import threading
import time

from .common import Database
from .contracts import timestamp


class ClockedDatabase(Database):
    def __init__(self, path, clock=time.time):
        super().__init__(path)
        self._clock, self._time = clock, threading.local()

    def clock(self):
        return (
            self._time.value if hasattr(self._time, "value") else timestamp(self._clock)
        )

    @contextmanager
    def transaction(self, write=False):
        with super().transaction(write) as db:
            try:
                if write:
                    self._time.value = timestamp(self._clock)
                yield db
            finally:
                if write and hasattr(self._time, "value"):
                    del self._time.value
