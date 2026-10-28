"""Compatibility facade and durable queue-to-storage consumer operations."""

import json
import logging
import time
from urllib.error import HTTPError

from .collector import Collector as Collector
from .collector import Dispatcher as Dispatcher
from .common import Error, digest
from .queue_adapter import QueueAdapter as QueueAdapter

LOGGER = logging.getLogger(__name__)


def consume_once(store, queue):
    source, batch = queue.fetch()
    if not batch["records"]:
        return False
    for record in batch["records"]:
        payload = record["value"]
        batch_id = digest(payload)
        try:
            body = json.loads(payload)
            if not isinstance(body, dict) or set(body) - {
                "batch_id",
                "samples",
                "partition",
            }:
                raise ValueError("invalid metrics queue envelope")
            batch_id = body["batch_id"]
            store.ingest(batch_id, body["samples"])
        except (ValueError, TypeError, KeyError) as exc:
            store.reject(digest(str(batch_id)), payload, type(exc).__name__)
        except Error as exc:
            # Temporary storage failures must leave the queue cursor unchanged.
            if not 400 <= exc.status < 500:
                raise
            store.reject(digest(str(batch_id)), payload, "sample rejected")
    queue.call("/commit", **source, next_offset=batch["next_offset"])
    return True


def storage_loop(store, queue, stop):
    last_compact = 0
    while not stop.is_set():
        error = ""
        try:
            worked = consume_once(store, queue)
            if time.monotonic() - last_compact >= 30:
                store.compact()
                last_compact = time.monotonic()
        except Exception as exc:
            worked = False
            error = type(exc).__name__
            LOGGER.warning("metric storage work failed; will retry")
            if isinstance(exc, HTTPError) and exc.code == 409:
                queue.member = None
        try:
            with store.transaction(True) as db:
                db.execute(
                    "INSERT INTO workers VALUES('storage',?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,error=excluded.error",
                    (store.clock(), error),
                )
        except Exception:
            LOGGER.warning("metric storage health update failed; will retry")
        if not worked:
            stop.wait(0.2)
