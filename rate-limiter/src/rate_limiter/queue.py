"""Optional FIFO execution pacing, separate from HTTP admission checks."""

from __future__ import annotations

import math
import queue
import threading
import time
from concurrent.futures import Future


class LeakyBucketQueue:
    """One owned worker starts queued callables at most ``rate`` times/sec.

    Submit is nonblocking and raises queue.Full when the waiting queue is full.
    Closing cancels waiting work and joins the current callback. Callbacks must
    have bounded run time; this class cannot interrupt arbitrary user code.
    This is a process-local execution queue, not durable distributed delivery.
    """

    def __init__(self, capacity: int, rate: float):
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        if isinstance(rate, bool) or not math.isfinite(rate) or rate <= 0:
            raise ValueError("rate must be finite and positive")
        self._queue = queue.Queue(maxsize=capacity)
        self._interval = 1 / rate
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    def start(self):
        with self._lock:
            if self._thread is not None or self._stop.is_set():
                raise RuntimeError("queue cannot be restarted")
            self._thread = threading.Thread(
                target=self._run, name="leaky-bucket-queue", daemon=False
            )
            self._thread.start()
        return self

    def submit(self, callback, *args, **kwargs) -> Future:
        if not callable(callback):
            raise ValueError("callback must be callable")
        with self._lock:
            if self._stop.is_set():
                raise RuntimeError("queue is closed")
            future = Future()
            self._queue.put_nowait((future, callback, args, kwargs))
            return future

    def _run(self):
        next_start = time.monotonic()
        while not self._stop.is_set():
            try:
                future, callback, args, kwargs = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                if self._stop.wait(max(0, next_start - time.monotonic())):
                    future.cancel()
                    continue
                if not future.set_running_or_notify_cancel():
                    continue
                next_start = time.monotonic() + self._interval
                try:
                    future.set_result(callback(*args, **kwargs))
                except BaseException as error:
                    future.set_exception(error)
            finally:
                self._queue.task_done()

    def close(self):
        with self._lock:
            self._stop.set()
            while True:
                try:
                    future, _, _, _ = self._queue.get_nowait()
                except queue.Empty:
                    break
                future.cancel()
                self._queue.task_done()
        if self._thread is threading.current_thread():
            raise RuntimeError("close must be called by the queue owner")
        if self._thread is not None:
            self._thread.join()

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()
