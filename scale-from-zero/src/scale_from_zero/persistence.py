"""Sample one clock value after acquiring a SQLite writer, per thread."""

from contextlib import contextmanager
import threading
import time

from .common import Database
from .contracts import timestamp


class ClockedDatabase(Database):
    def __init__(self, path, clock=time.time):
        super().__init__(path)
        self.clock, self._time = clock, threading.local()

    def now(self):
        return (
            self._time.value
            if hasattr(self._time, "value")
            else timestamp(self.clock())
        )

    @contextmanager
    def transaction(self):
        with super().transaction() as db:
            self._time.value = timestamp(self.clock())
            try:
                yield db
            finally:
                del self._time.value
