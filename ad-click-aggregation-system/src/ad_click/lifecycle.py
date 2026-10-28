"""Owned aggregation polling with visible failures and durable-position retry."""

import json
import logging
import threading
import time

from .common import dumps

_LOG = logging.getLogger(__name__)


class WorkerLoop:
    def __init__(self, config, store):
        self.config, self.store = json.loads(dumps(config)), store
        self.stop = threading.Event()
        self.error = "starting"
        self.updated = 0
        self._lifecycle = threading.Lock()
        self.thread = threading.Thread(target=self.run, name="ad-click-aggregation")

    def run_once(self):
        # Lazy import prevents HTTP routing/service composition import cycles.
        from .service import log_page

        batch = log_page(self.config, self.store.partition, self.store.status()["seq"])
        self.store.apply(batch)
        if not batch["entries"]:
            self.store.idle()
        self.error, self.updated = None, time.time()
        return bool(batch["entries"])

    def run(self):
        while not self.stop.is_set():
            try:
                worked = self.run_once()
                self.stop.wait(0.02 if worked else 0.1)
            except Exception:
                # The input cursor belongs to the durable apply transaction;
                # retry it, never skip a corrupt record or expose private text.
                self.error = "aggregation poll failed; durable position retained"
                _LOG.warning(self.error)
                self.stop.wait(0.2)

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
