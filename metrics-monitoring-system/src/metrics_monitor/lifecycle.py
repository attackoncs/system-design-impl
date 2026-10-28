"""Owned periodic work with visible failures and complete shutdown drain."""

import logging
import threading

from .transport import seconds

_LOG = logging.getLogger(__name__)


class Worker:
    def __init__(self, action, interval, *, name="metrics-worker", continuous=False):
        if not callable(action):
            raise ValueError("worker action required")
        self.action, self.interval = action, seconds(interval)
        self.continuous = continuous
        self.stop = threading.Event()
        self._error = None
        self._lifecycle = threading.Lock()
        self.thread = threading.Thread(target=self.run, name=name)

    @property
    def last_error(self):
        return self._error or getattr(
            getattr(self.action, "__self__", None), "last_error", None
        )

    def run(self):
        while not self.stop.is_set():
            worked = False
            try:
                worked = self.action()
                self._error = None
            except Exception:
                self._error = "background operation failed; will retry"
                _LOG.warning(self._error)
            if not self.continuous or not worked:
                self.stop.wait(self.interval)

    def start(self):
        with self._lifecycle:
            if self.stop.is_set():
                raise RuntimeError("worker is closed")
            self.thread.start()
        return self

    def close(self):
        if self.thread.ident == threading.get_ident():
            raise RuntimeError("worker cannot join itself")
        with self._lifecycle:
            self.stop.set()
            started = self.thread.ident is not None
        if started:
            self.thread.join()
