"""Ownership of synchronous operations that release locks for external I/O."""

from contextlib import contextmanager
from functools import wraps
import threading


class Operations:
    def __init__(self, lock):
        self.condition = threading.Condition(lock)
        self.local = threading.local()
        self.active = 0
        self.closing = False

    @contextmanager
    def enter(self):
        with self.condition:
            depth = getattr(self.local, "depth", 0)
            if self.closing and not depth:
                raise RuntimeError("video service is closing or closed")
            self.local.depth = depth + 1
            self.active += 1
        try:
            yield
        finally:
            with self.condition:
                self.local.depth -= 1
                self.active -= 1
                self.condition.notify_all()

    def drain(self):
        if getattr(self.local, "depth", 0):
            raise RuntimeError("cannot close from inside an active operation")
        self.closing = True
        while self.active:
            self.condition.wait()


def owned(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._operations.enter():
            return method(self, *args, **kwargs)

    return call
