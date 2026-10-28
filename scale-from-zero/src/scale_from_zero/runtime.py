"""Thread-local request I/O budgets without coupling domain logic to HTTP."""

from contextlib import contextmanager
import threading
import time


class RequestContext:
    def __init__(self):
        self.local = threading.local()

    def options(self):
        return {
            "deadline": getattr(self.local, "deadline", time.monotonic() + 8),
            "cancel": getattr(self.local, "cancel", None),
        }

    @contextmanager
    def operation(self, deadline, cancel=None):
        old = self.local.__dict__.copy()
        self.local.deadline, self.local.cancel = deadline, cancel
        try:
            yield
        finally:
            self.local.__dict__.clear()
            self.local.__dict__.update(old)
