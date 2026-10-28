"""Owned periodic maintenance: retry failures and join accepted I/O on close."""

import logging
import math
import threading

_LOG = logging.getLogger(__name__)


class Maintenance:
    def __init__(self, action, interval=30):
        try:
            valid = (
                type(interval) in (int, float)
                and math.isfinite(interval)
                and 0 < interval <= 3600
            )
        except OverflowError:
            valid = False
        if not callable(action) or not valid:
            raise ValueError("invalid maintenance configuration")
        self.action, self.interval = action, interval
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="maps-maintenance")
        self.last_error = None

    def run(self):
        while not self.stop.wait(self.interval):
            try:
                self.action()
                self.last_error = None
            except Exception:
                self.last_error = "maps maintenance failed; will retry"
                _LOG.warning(self.last_error)

    def start(self):
        if self.stop.is_set():
            raise RuntimeError("maintenance is closed")
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if self.thread.ident == threading.get_ident():
            raise RuntimeError("maintenance cannot join itself")
        if self.thread.ident is not None:
            self.thread.join()
