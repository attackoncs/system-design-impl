"""Durable, bounded collector outbox with fenced partition-FIFO claims."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import logging
import math
import threading
import time
import uuid
import zlib

from .common import (
    Database,
    Error,
    digest,
    dumps,
    http_json,
    identifier,
    labels,
    sample,
)

LOGGER = logging.getLogger(__name__)


def integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("integer outside reference limit")
    return value


def finite(value, minimum, maximum):
    if (
        type(value) not in (int, float)
        or not minimum <= value <= maximum
        or not math.isfinite(value)
    ):
        raise ValueError("number outside reference limit")
    return value


def partition_for(body, count, by):
    key = body["metric"] if by == "metric" else [body["metric"], body["labels"]]
    return zlib.crc32(dumps(key).encode()) % count


class Collector(Database):
    def __init__(
        self,
        path,
        max_bytes=16 * 1024 * 1024,
        clock=time.time,
        *,
        partitions=1,
        partition_by="metric",
    ):
        integer(max_bytes, 1, 1024 * 1024 * 1024)
        integer(partitions, 1, 64)
        if partition_by not in ("metric", "series") or not callable(clock):
            raise ValueError("invalid collector configuration")
        super().__init__(path)
        self.max_bytes, self.clock = max_bytes, clock
        self.partitions, self.partition_by = partitions, partition_by
        self.last_error = None
        with closing(self.connect()) as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,hash TEXT,response TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,payload TEXT,status TEXT,attempts INTEGER,due REAL);
            CREATE TABLE IF NOT EXISTS scrapes(target TEXT PRIMARY KEY,updated REAL,error TEXT);
            CREATE TABLE IF NOT EXISTS spool_state(id INTEGER PRIMARY KEY,bytes INTEGER NOT NULL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(outbox)")}
            for column, declaration in (
                ("partition", "INTEGER NOT NULL DEFAULT 0"),
                ("owner", "TEXT"),
                ("lease", "REAL NOT NULL DEFAULT 0"),
            ):
                if column not in columns:
                    db.execute(f"ALTER TABLE outbox ADD COLUMN {column} {declaration}")
            db.execute(
                "CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(status,partition)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS receipt_age ON receipts(created)")
            db.execute(
                "INSERT OR IGNORE INTO spool_state SELECT 1,coalesce(sum(length(CAST(payload AS BLOB))),0) FROM outbox WHERE status='pending'"
            )
            db.commit()

    def _now(self):
        return finite(self.clock(), 0, 1e15)

    def enqueue(self, samples, request_id):
        identifier(request_id)
        if not isinstance(samples, list) or not 1 <= len(samples) <= 1000:
            raise ValueError("push requires 1..1000 samples")
        normalized = [sample(body) for body in list(samples)]
        fingerprint = digest(normalized)
        grouped = {}
        for body in normalized:
            grouped.setdefault(
                partition_for(body, self.partitions, self.partition_by), []
            ).append(body)
        payloads = []
        for partition, values in grouped.items():
            chunk = []
            for body in values:
                if (
                    len(
                        dumps(
                            {
                                "batch_id": "0" * 64,
                                "partition": partition,
                                "samples": [body],
                            }
                        ).encode()
                    )
                    > 12000
                ):
                    raise ValueError("sample too large for queue transport")
                candidate = chunk + [body]
                if chunk and (
                    len(candidate) > 100
                    or len(
                        dumps(
                            {
                                "batch_id": "0" * 64,
                                "partition": partition,
                                "samples": candidate,
                            }
                        ).encode()
                    )
                    > 12000
                ):
                    payloads.append((partition, chunk))
                    chunk = []
                chunk.append(body)
            if chunk:
                payloads.append((partition, chunk))
        captured = []
        for index, (partition, chunk) in enumerate(payloads):
            batch_id = digest([request_id, index])
            payload = dumps(
                {"batch_id": batch_id, "partition": partition, "samples": chunk}
            )
            captured.append((batch_id, partition, payload))
        total = sum(len(payload.encode()) for _, _, payload in captured)
        with self.transaction(True) as db:
            now = self._now()
            replay = db.execute(
                "SELECT * FROM receipts WHERE id=?", (request_id,)
            ).fetchone()
            if replay:
                if replay["hash"] != fingerprint:
                    raise Error(409, "push ID payload conflict")
                return json.loads(replay["response"])
            used = db.execute("SELECT bytes FROM spool_state WHERE id=1").fetchone()[0]
            if used + total > self.max_bytes:
                raise Error(429, "durable collector backlog capacity exceeded")
            db.executemany(
                "INSERT INTO outbox(id,payload,status,attempts,due,partition) VALUES(?,?,'pending',0,?,?)",
                [
                    (batch_id, payload, now, partition)
                    for batch_id, partition, payload in captured
                ],
            )
            db.execute("UPDATE spool_state SET bytes=bytes+? WHERE id=1", (total,))
            result = {
                "request_id": request_id,
                "queued_chunks": len(captured),
                "samples": len(normalized),
                "stored_in_tsdb": False,
            }
            db.execute(
                "INSERT INTO receipts VALUES(?,?,?,?)",
                (request_id, fingerprint, dumps(result), now),
            )
            # Bounded housekeeping keeps an accepted hot write from deleting all history.
            db.execute(
                "DELETE FROM receipts WHERE id IN (SELECT id FROM receipts WHERE created<? LIMIT 500)",
                (now - 14 * 86400,),
            )
            db.execute(
                "DELETE FROM outbox WHERE rowid IN (SELECT rowid FROM outbox WHERE status='sent' AND due<? LIMIT 500)",
                (now - 14 * 86400,),
            )
            return result

    def flush_once(self, queue):
        owner = uuid.uuid4().hex
        with self.transaction(True) as db:
            now = self._now()
            item = db.execute(
                """SELECT o.* FROM outbox o WHERE o.status='pending' AND o.due<=? AND o.lease<=?
                AND NOT EXISTS(SELECT 1 FROM outbox p WHERE p.status='pending' AND p.partition=o.partition AND p.rowid<o.rowid)
                ORDER BY o.rowid LIMIT 1""",
                (now, now),
            ).fetchone()
            if not item:
                return False
            db.execute(
                "UPDATE outbox SET owner=?,lease=? WHERE id=?",
                (owner, now + 120, item["id"]),
            )
        try:
            queue.publish(item["payload"])
        except Exception:
            with self.transaction(True) as db:
                now = self._now()
                db.execute(
                    "UPDATE outbox SET attempts=attempts+1,due=?,owner=NULL,lease=0 WHERE id=? AND owner=?",
                    (now + min(60, 2 ** min(item["attempts"], 6)), item["id"], owner),
                )
            self.last_error = "queue publication pending; will retry"
            LOGGER.warning(self.last_error)
            return False
        with self.transaction(True) as db:
            updated = db.execute(
                "UPDATE outbox SET status='sent',payload=NULL,due=?,owner=NULL,lease=0 WHERE id=? AND owner=?",
                (self._now(), item["id"], owner),
            ).rowcount
            if updated:
                db.execute(
                    "UPDATE spool_state SET bytes=bytes-? WHERE id=1",
                    (len(item["payload"].encode()),),
                )
        self.last_error = None
        return bool(updated)

    def scrape(self, target):
        instance = identifier(target["instance"])
        timeout = finite(target.get("timeout", 5), 0.05, 30)
        retries = integer(target.get("retries", 0), 0, 2)
        trusted = labels(target.get("labels", {}))
        deadline = time.monotonic() + timeout
        error, up, captured = "", 0, []
        for attempt in range(retries + 1):
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("scrape budget expired")
                result = http_json(
                    target["url"], token=target.get("token"), timeout=remaining
                )
                samples = result["samples"]
                if not isinstance(samples, list) or not 1 <= len(samples) <= 1000:
                    raise ValueError("exporter sample list required")
                captured = [
                    sample(
                        {
                            **body,
                            "labels": {
                                **body.get("labels", {}),
                                **trusted,
                                "instance": instance,
                            },
                        }
                    )
                    for body in samples
                ]
                error, up = "", 1
                break
            except Exception as exc:
                error = type(exc).__name__
                if attempt == retries:
                    break
        captured.append(
            {
                "metric": "collector_up",
                "labels": {**trusted, "instance": instance},
                "type": "gauge",
                "timestamp": self._now(),
                "value": up,
            }
        )
        response = self.enqueue(captured, uuid.uuid4().hex)
        with self.transaction(True) as db:
            db.execute(
                "INSERT INTO scrapes VALUES(?,?,?) ON CONFLICT(target) DO UPDATE SET updated=excluded.updated,error=excluded.error",
                (instance, self._now(), error),
            )
        return response

    def health(self):
        with self.transaction() as db:
            return {
                "pending_chunks": db.execute(
                    "SELECT count(*) FROM outbox WHERE status='pending'"
                ).fetchone()[0],
                "pending_bytes": db.execute(
                    "SELECT bytes FROM spool_state WHERE id=1"
                ).fetchone()[0],
                "scrapes": [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM scrapes ORDER BY target LIMIT 1000"
                    )
                ],
                "dispatch_error": self.last_error,
            }


class Dispatcher:
    """Bounded parallel outbox claims; one owner must drain before shutdown."""

    def __init__(self, collector, queue, workers=4):
        integer(workers, 1, 32)
        self.collector, self.queue, self.workers = collector, queue, workers
        self._lock = threading.Lock()
        self._closed = False
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="metric-outbox"
        )

    def run_once(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("dispatcher closed")
            futures = [
                self._executor.submit(self.collector.flush_once, self.queue)
                for _ in range(self.workers)
            ]
            results, failure = [], None
            for future in futures:
                try:
                    results.append(future.result())
                except Exception as exc:
                    failure = failure or exc
            if failure:
                raise failure
            return any(results)

    def close(self):
        with self._lock:
            self._closed = True
            self._executor.shutdown(wait=True)
