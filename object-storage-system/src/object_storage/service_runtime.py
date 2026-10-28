"""Own coordinator admission, deadlines, replication threads and GC exclusion."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading
import time
import math

from .common import Error, integer


class Cancellation:
    def __init__(self, *events):
        self.events = tuple(event for event in events if event is not None)

    def is_set(self):
        return any(event.is_set() for event in self.events)


class Runtime:
    def __init__(self, concurrency=8, io_workers=12):
        integer(concurrency, "coordinator concurrency", 1, 128)
        integer(io_workers, "replica workers", 1, 128)
        self.pool = ThreadPoolExecutor(
            max_workers=io_workers, thread_name_prefix="object-replica"
        )
        self.slots = threading.BoundedSemaphore(concurrency)
        self.closing = threading.Event()
        self.local = threading.local()
        self.condition = threading.Condition(threading.RLock())
        self.readers = {}
        self.writer = None
        self.depth = self.waiting = 0

    def context(self):
        return (
            getattr(self.local, "deadline", time.monotonic() + 15),
            Cancellation(self.closing, getattr(self.local, "cancel", None)),
        )

    @staticmethod
    def remaining(deadline, cancel):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or cancel.is_set():
            raise Error(503, "coordinator operation cancelled or deadline exceeded")
        return remaining

    @contextmanager
    def operation(self, *, deadline, cancel=None):
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("finite monotonic operation deadline required")
        combined = Cancellation(self.closing, cancel)
        while not self.slots.acquire(
            timeout=min(0.05, self.remaining(deadline, combined))
        ):
            pass
        old = (
            getattr(self.local, "deadline", None),
            getattr(self.local, "cancel", None),
        )
        self.local.deadline, self.local.cancel = deadline, combined
        try:
            self.remaining(deadline, combined)
            yield
        finally:
            self.local.deadline, self.local.cancel = old
            if old[0] is None:
                del self.local.deadline
                del self.local.cancel
            self.slots.release()

    @contextmanager
    def guard(self, exclusive=False):
        owner = threading.get_ident()
        deadline, cancel = self.context()
        with self.condition:
            if exclusive and self.writer == owner:
                self.depth += 1
            elif exclusive:
                if owner in self.readers:
                    raise RuntimeError("cannot upgrade coordinator read ownership")
                self.waiting += 1
                try:
                    while self.writer is not None or self.readers:
                        self.condition.wait(min(0.05, self.remaining(deadline, cancel)))
                    self.remaining(deadline, cancel)
                    self.writer, self.depth = owner, 1
                finally:
                    self.waiting -= 1
                    self.condition.notify_all()
            else:
                while self.writer not in (None, owner) or (
                    self.writer != owner and self.waiting and owner not in self.readers
                ):
                    self.condition.wait(min(0.05, self.remaining(deadline, cancel)))
                self.remaining(deadline, cancel)
                self.readers[owner] = self.readers.get(owner, 0) + 1
        try:
            yield
        finally:
            with self.condition:
                if exclusive:
                    self.depth -= 1
                    if not self.depth:
                        self.writer = None
                else:
                    self.readers[owner] -= 1
                    if not self.readers[owner]:
                        del self.readers[owner]
                self.condition.notify_all()

    def close(self):
        self.closing.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
