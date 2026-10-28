"""Owned daily UTC publication, independent of client/read traffic."""

from datetime import datetime, timedelta, timezone
import logging
import math
import threading

_LOG = logging.getLogger(__name__)


class DailyPublisher:
    """Poll the publication deadline and drain an in-progress build on close.

    The interval bounds how often wall-clock changes are observed. Waiting is
    interruptible, but SQLite/index work already in progress is allowed to finish.
    Multiple publishers still coordinate through Store's publication fences.
    """

    def __init__(self, store, interval=1):
        try:
            valid = (
                type(interval) in (int, float)
                and math.isfinite(interval)
                and 1 <= interval <= 86400
            )
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError(
                "publication interval must be finite and in 1..86400 seconds"
            )
        if store.readonly:
            raise ValueError("replicas cannot run a daily publisher")
        self.store, self.interval = store, interval
        self.stop = threading.Event()
        self.last_error = None
        self.last_snapshot = None
        self.thread = threading.Thread(target=self.run, name="proximity-publication")

    def run_once(self):
        result = self.store.publish()
        self.last_snapshot = dict(result)
        self.last_error = None
        return result

    def _delay(self):
        now = datetime.fromtimestamp(self.store.clock(), timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return min(self.interval, max(0.001, (tomorrow - now).total_seconds()))

    def run(self):
        while not self.stop.is_set():
            try:
                self.run_once()
                delay = self._delay()
            except Exception:
                # Failed construction/publication leaves the old active data
                # intact. The next tick retries from durable drafts and metadata.
                self.last_error = "daily snapshot publication failed"
                _LOG.exception(self.last_error)
                delay = self.interval
            if self.stop.wait(delay):
                return

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if self.thread.ident == threading.get_ident():
            raise RuntimeError("daily publisher cannot join itself")
        if self.thread.ident is not None:
            self.thread.join()
