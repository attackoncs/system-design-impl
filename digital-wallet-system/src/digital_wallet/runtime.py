"""Bounded operation contexts and deadline-aware ownership of money mutations."""

from contextlib import contextmanager
import threading
import time

from .common import Error


class Cancellation:
    def __init__(self, *events):
        self.events = [event for event in events if event is not None]

    def is_set(self):
        return any(event.is_set() for event in self.events)


class Runtime:
    def __init__(self):
        self.lock = threading.RLock()
        self.local = threading.local()
        self.closing = threading.Event()
        self.slots = threading.BoundedSemaphore(16)

    def context(self):
        return {
            "deadline": getattr(self.local, "deadline", time.monotonic() + 10),
            "cancel": Cancellation(self.closing, getattr(self.local, "cancel", None)),
        }

    @staticmethod
    def live(deadline, cancel):
        if cancel.is_set() or deadline <= time.monotonic():
            raise Error(
                503, "wallet operation cancelled or expired; retain transaction_id"
            )

    @contextmanager
    def operation(self, deadline, cancel=None):
        combined = Cancellation(self.closing, cancel)
        self.live(deadline, combined)
        if not self.slots.acquire(False):
            raise Error(503, "wallet operation capacity exhausted")
        old = self.local.__dict__.copy()
        self.local.deadline, self.local.cancel = deadline, combined
        try:
            yield
        finally:
            self.local.__dict__.clear()
            self.local.__dict__.update(old)
            self.slots.release()

    @contextmanager
    def mutation(self):
        context = self.context()
        while True:
            self.live(**context)
            if self.lock.acquire(
                timeout=max(0, min(0.05, context["deadline"] - time.monotonic()))
            ):
                break
        try:
            self.live(**context)
            yield
        finally:
            self.lock.release()
