"""Periodic offline builds; failed work leaves the published generation intact."""

import math
import sqlite3
import threading
import time

from search_autocomplete.store import Unavailable

WEEK = 7 * 86400


def _duration(value, name):
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be positive and finite")
    return value


class SnapshotWorker:
    """Build a trailing window immediately, then periodically until stopped.

    The first window ends at the caller's clock; it is not a calendar-week
    boundary. Scheduling state lives in this process. Publication stays durable.
    """

    def __init__(
        self,
        store,
        *,
        shards=1,
        interval=WEEK,
        window=WEEK,
        retry_delay=5,
        clock=time.time,
        purge_blocked=False,
    ):
        if (
            not isinstance(shards, int)
            or isinstance(shards, bool)
            or not 1 <= shards <= 256
        ):
            raise ValueError("invalid shard count")
        self.store = store
        self.shards = shards
        self.interval = _duration(interval, "interval")
        self.window = _duration(window, "window")
        self.retry_delay = _duration(retry_delay, "retry_delay")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.clock = clock
        if not isinstance(purge_blocked, bool):
            raise ValueError("purge_blocked must be a boolean")
        self.purge_blocked = purge_blocked

    def run_once(self, end=None):
        """Aggregate, construct and atomically publish one complete generation."""
        end = self.clock() if end is None else end
        _duration(end, "window end")
        if self.purge_blocked:
            self.store.purge_blocked()
        return self.store.build(max(0, end - self.window), end, self.shards)

    def run(self, stop=None, *, on_publish=None, on_error=None):
        """Use an interruptible wait; retry transient store/publication failures.

        Stop prevents new work and waits for an in-progress atomic build to finish.
        Callbacks run in the worker's thread; callback failures reach the caller.
        """
        stop = threading.Event() if stop is None else stop
        while not stop.is_set():
            try:
                version = self.run_once()
            except (Unavailable, sqlite3.Error) as error:
                if on_error is not None:
                    on_error(error)
                delay = self.retry_delay
            else:
                if on_publish is not None:
                    on_publish(version)
                delay = self.interval
            if stop.wait(delay):
                return
