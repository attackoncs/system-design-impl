"""Single-iteration background operations and their owned thread lifecycle."""

import logging
import threading
import time

from .contracts import fields

from .common import request

logger = logging.getLogger(__name__)


class PeriodicWorker:
    """Run bounded-I/O steps without overlap; stop and join before releasing owners."""

    def __init__(self, step, name, interval=0.04):
        self._step = step
        self._stop = threading.Event()
        self._interval = interval
        self._thread = threading.Thread(target=self._run, name=name, daemon=False)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._step()
            except Exception:
                logger.exception("Background step failed: %s", self._thread.name)
                if self._stop.wait(0.1):
                    return
            self._stop.wait(self._interval)

    def start(self):
        self._thread.start()

    @property
    def cancel(self):
        return self._stop

    def stop(self):
        self._stop.set()

    def close(self):
        self.stop()
        if self._thread.ident is not None:
            self._thread.join()


def replicate_once(
    store, primary, token, transport=request, *, deadline=None, cancel=None
):
    after = store.status()["seq"]
    feed = transport(
        primary + "/internal/changes?after=" + str(after),
        token,
        timeout=0.5,
        deadline=deadline,
        cancel=cancel,
    )
    store.apply_feed(feed)


def consume_local_once(store, consumer):
    job = store.claim(lease_seconds=5)
    if job and consumer.consume(job):
        store.ack(job)


def consume_remote_once(
    primaries, token, consumer, transport=request, *, deadline=None, cancel=None
):
    for url in primaries:
        if cancel is not None and cancel.is_set():
            return
        budget = (
            min(deadline, time.monotonic() + 2)
            if deadline is not None
            else time.monotonic() + 2
        )
        try:
            reply = transport(
                url + "/internal/claim",
                token,
                {},
                timeout=0.5,
                deadline=budget,
                cancel=cancel,
            )
            fields(reply, {"job"})
            job = reply["job"]
            if job and consumer.consume(job):
                transport(
                    url + "/internal/ack",
                    token,
                    {"job": job},
                    timeout=0.5,
                    deadline=budget,
                    cancel=cancel,
                )
        except Exception:
            # Isolate each shard: an unavailable owner must not starve others.
            logger.exception("Task delivery failed for shard %s", url)
