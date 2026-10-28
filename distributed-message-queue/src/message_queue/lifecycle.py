"""Periodic maintenance with visible failures and complete accepted-work drain."""

import logging
import threading

from .transport import seconds

_LOG = logging.getLogger(__name__)


class Maintenance:
    def __init__(self, action, interval=1):
        if not callable(action):
            raise ValueError("maintenance action must be callable")
        self.action, self.interval = action, seconds(interval)
        self.stop = threading.Event()
        self._lifecycle = threading.Lock()
        self.thread = threading.Thread(target=self.run, name="queue-maintenance")
        self.last_error = None

    def run(self):
        while not self.stop.wait(self.interval):
            try:
                self.action()
                self.last_error = None
            except Exception:
                self.last_error = "queue maintenance failed; will retry"
                _LOG.warning(self.last_error)

    def start(self):
        with self._lifecycle:
            if self.stop.is_set():
                raise RuntimeError("maintenance is closed")
            self.thread.start()
        return self

    def close(self):
        with self._lifecycle:
            self.stop.set()
            if self.thread.ident == threading.get_ident():
                raise RuntimeError("maintenance cannot join itself")
            started = self.thread.ident is not None
        if started:
            self.thread.join()
