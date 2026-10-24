"""SQLite queue and audit log for a recoverable, at-least-once pipeline.

Acceptance and the QUEUED audit row share a transaction. A claimed row remains
in the database until acknowledged; leases recover work after process death.
SQLite is a local reference backend, not a distributed message broker.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime
import json
import math
import pathlib
import sqlite3
import time
import uuid

from notification_system.exceptions import DuplicateNotificationError, QueueFullError
from notification_system.log import NotificationLogBackend
from notification_system.models import (
    Channel,
    LogEntry,
    NotificationRequest,
    NotificationStatus,
    NotificationTask,
)
from notification_system.queue import MessageQueue


def _encode(task: NotificationTask) -> str:
    value = dataclasses.asdict(task)
    value["channel"] = task.channel.value
    value["request"]["channel"] = task.request.channel.value
    value["created_at"] = task.created_at.isoformat()
    value["scheduled_at"] = task.scheduled_at.isoformat() if task.scheduled_at else None
    value["receipt"] = None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _decode(raw: str) -> NotificationTask:
    value = json.loads(raw)
    value["channel"] = Channel(value["channel"])
    value["request"]["channel"] = Channel(value["request"]["channel"])
    value["request"] = NotificationRequest(**value["request"])
    value["created_at"] = datetime.datetime.fromisoformat(value["created_at"])
    if value["scheduled_at"]:
        value["scheduled_at"] = datetime.datetime.fromisoformat(value["scheduled_at"])
    value["endpoints"] = tuple(value["endpoints"])
    value["completed_endpoints"] = tuple(value["completed_endpoints"])
    value["rejected_endpoints"] = tuple(value.get("rejected_endpoints", ()))
    return NotificationTask(**value)


class _Database:
    def __init__(self, path) -> None:
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS notification_jobs (
                    id TEXT PRIMARY KEY, channel TEXT NOT NULL, payload TEXT NOT NULL,
                    available REAL NOT NULL, receipt TEXT, lease_until REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS jobs_ready
                    ON notification_jobs(channel, available, lease_until);
                CREATE TABLE IF NOT EXISTS notification_events (
                    event_id TEXT NOT NULL, recipient_id TEXT NOT NULL,
                    channel TEXT NOT NULL, notification_id TEXT NOT NULL,
                    PRIMARY KEY(event_id, recipient_id, channel)
                );
                CREATE TABLE IF NOT EXISTS notification_log (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, notification_id TEXT NOT NULL,
                    recipient_id TEXT NOT NULL, channel TEXT NOT NULL, content_summary TEXT NOT NULL,
                    timestamp REAL NOT NULL, status TEXT NOT NULL, attempt INTEGER NOT NULL,
                    error_details TEXT
                );
                CREATE INDEX IF NOT EXISTS log_notification
                    ON notification_log(notification_id, seq);
            """)

    @contextlib.contextmanager
    def connection(self):
        with contextlib.closing(sqlite3.connect(self.path, timeout=5.0)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db

    @staticmethod
    def log(db, task, status, details=None):
        db.execute(
            "INSERT INTO notification_log(notification_id, recipient_id, channel, "
            "content_summary, timestamp, status, attempt, error_details) VALUES(?,?,?,?,?,?,?,?)",
            (
                task.notification_id,
                task.request.recipient_id,
                task.channel.value,
                task.request.body[:100],
                time.time(),
                status.value,
                task.attempt + 1,
                details,
            ),
        )


class SQLiteMessageQueue(MessageQueue):
    """Durable per-channel jobs with fenced leases and atomic acknowledgements.

    All queue mutations run in a thread. Depth methods are synchronous monitoring
    snapshots. Multiple queue instances can share the file; rate limits and
    preferences are still process-local unless callers provide shared stores.
    """

    def __init__(self, path, max_depth: int = 10000, lease_seconds: float = 30.0):
        super().__init__(max_depth)
        if (
            type(lease_seconds) not in (int, float)
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be finite and positive")
        self.lease_seconds = lease_seconds
        self._db = _Database(path)
        self._io = asyncio.Lock()

    async def _run(self, operation, *args):
        async with self._io:
            running = asyncio.create_task(asyncio.to_thread(operation, *args))
            try:
                return await asyncio.shield(running)
            except asyncio.CancelledError:
                # Keep the serialization lock until the transaction ends.
                await running
                raise

    def _enqueue(self, task):
        with self._db.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            request = task.request
            if request.event_id is not None:
                original = db.execute(
                    "SELECT notification_id FROM notification_events WHERE event_id=? AND recipient_id=? AND channel=?",
                    (request.event_id, request.recipient_id, task.channel.value),
                ).fetchone()
                if original is not None:
                    raise DuplicateNotificationError(original[0])
            count = db.execute(
                "SELECT count(*) FROM notification_jobs WHERE channel=?",
                (task.channel.value,),
            ).fetchone()[0]
            if count >= self._max_depth:
                raise QueueFullError(task.channel.value, self._max_depth)
            available = (
                task.scheduled_at.timestamp() if task.scheduled_at else time.time()
            )
            db.execute(
                "INSERT INTO notification_jobs(id,channel,payload,available) VALUES(?,?,?,?)",
                (task.notification_id, task.channel.value, _encode(task), available),
            )
            self._db.log(db, task, NotificationStatus.QUEUED)
            if request.event_id is not None:
                db.execute(
                    "INSERT INTO notification_events(event_id,recipient_id,channel,notification_id) VALUES(?,?,?,?)",
                    (
                        request.event_id,
                        request.recipient_id,
                        task.channel.value,
                        task.notification_id,
                    ),
                )

    async def enqueue(self, task):
        await self._run(self._enqueue, task)

    def _lookup_event(self, recipient_id, channel, event_id):
        with self._db.connection() as db:
            row = db.execute(
                "SELECT notification_id FROM notification_events WHERE event_id=? AND recipient_id=? AND channel=?",
                (event_id, recipient_id, channel.value),
            ).fetchone()
        return row[0] if row is not None else None

    async def event_notification_id(self, recipient_id, channel, event_id):
        return await self._run(self._lookup_event, recipient_id, channel, event_id)

    def _claim(self, channel):
        now = time.time()
        with self._db.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM notification_jobs WHERE channel=? AND available<=? "
                "AND (receipt IS NULL OR lease_until<=?) ORDER BY available,rowid LIMIT 1",
                (channel.value, now, now),
            ).fetchone()
            if row is None:
                return None
            task = _decode(row["payload"])
            task.receipt = uuid.uuid4().hex
            db.execute(
                "UPDATE notification_jobs SET receipt=?,lease_until=? WHERE id=?",
                (task.receipt, now + self.lease_seconds, task.notification_id),
            )
            return task

    async def claim(self, channel):
        while True:
            operation = asyncio.create_task(self._run(self._claim, channel))
            try:
                task = await asyncio.shield(operation)
            except asyncio.CancelledError:
                # A cancelled database claim may already have committed.
                task = await operation
                if task is not None:
                    await self.reschedule(task)
                raise
            if task is not None:
                return task
            await asyncio.sleep(min(0.05, self.lease_seconds / 3))

    async def dequeue(self, channel):
        """Compatibility pop; consumers needing recovery must use claim/ack."""
        task = await self.claim(channel)
        await self.ack(task)
        return task

    def dequeue_nowait(self, channel):
        raise RuntimeError("SQLiteMessageQueue requires async claim/dequeue")

    def _ack(self, task, status, details):
        with self._db.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "DELETE FROM notification_jobs WHERE id=? AND receipt=? AND lease_until>?",
                (task.notification_id, task.receipt, time.time()),
            ).rowcount
            if not changed:
                raise RuntimeError("Notification lease was lost before acknowledgement")
            if status is not None:
                self._db.log(db, task, status, details)

    async def ack(self, task, status=None, details=None):
        await self._run(self._ack, task, status, details)

    def _renew(self, task):
        with self._db.connection() as db:
            now = time.time()
            return bool(
                db.execute(
                    "UPDATE notification_jobs SET lease_until=? WHERE id=? AND receipt=? AND lease_until>?",
                    (now + self.lease_seconds, task.notification_id, task.receipt, now),
                ).rowcount
            )

    async def renew(self, task):
        return await self._run(self._renew, task)

    def _reschedule(self, task, delay):
        if not task.receipt:
            self._enqueue(task)
            return
        with self._db.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE notification_jobs SET payload=?,available=?,receipt=NULL,lease_until=0 "
                "WHERE id=? AND receipt=? AND lease_until>?",
                (
                    _encode(task),
                    time.time() + delay,
                    task.notification_id,
                    task.receipt,
                    time.time(),
                ),
            ).rowcount
            if not changed:
                raise RuntimeError("Notification lease was lost before rescheduling")
            self._db.log(db, task, NotificationStatus.QUEUED, "rescheduled")

    async def reschedule(self, task, delay=0.0):
        task.scheduled_at = datetime.datetime.now(
            datetime.timezone.utc
        ) + datetime.timedelta(seconds=delay)
        await self._run(self._reschedule, task, delay)

    def _count(self, channel, in_flight):
        predicate = (
            "receipt IS NOT NULL AND lease_until>?"
            if in_flight
            else "(receipt IS NULL OR lease_until<=?)"
        )
        with self._db.connection() as db:
            return db.execute(
                f"SELECT count(*) FROM notification_jobs WHERE channel=? AND {predicate}",
                (channel.value, time.time()),
            ).fetchone()[0]

    def depth(self, channel):
        return self._count(channel, False)

    def in_flight(self, channel):
        return self._count(channel, True)


class SQLiteNotificationLog(NotificationLogBackend):
    """Queryable durable audit history, including terminal queue acknowledgements."""

    def __init__(self, path):
        self._db = _Database(path)

    def append(self, entry):
        with self._db.connection() as db:
            db.execute(
                "INSERT INTO notification_log(notification_id,recipient_id,channel,content_summary,timestamp,status,attempt,error_details) VALUES(?,?,?,?,?,?,?,?)",
                (
                    entry.notification_id,
                    entry.recipient_id,
                    entry.channel.value,
                    entry.content_summary,
                    entry.timestamp.timestamp(),
                    entry.status.value,
                    entry.attempt,
                    entry.error_details,
                ),
            )

    def query(
        self,
        notification_id=None,
        recipient_id=None,
        channel=None,
        status=None,
        start_time=None,
        end_time=None,
    ):
        clauses, values = [], []
        for name, value in (
            ("notification_id", notification_id),
            ("recipient_id", recipient_id),
            ("channel", channel.value if channel else None),
            ("status", status.value if status else None),
        ):
            if value is not None:
                clauses.append(f"{name}=?")
                values.append(value)
        for operator, value in ((">=", start_time), ("<=", end_time)):
            if value is not None:
                clauses.append(f"timestamp {operator} ?")
                values.append(value.timestamp())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._db.connection() as db:
            rows = db.execute(
                "SELECT * FROM notification_log" + where + " ORDER BY seq", values
            ).fetchall()
        return [
            LogEntry(
                row["notification_id"],
                row["recipient_id"],
                Channel(row["channel"]),
                row["content_summary"],
                datetime.datetime.fromtimestamp(
                    row["timestamp"], datetime.timezone.utc
                ),
                NotificationStatus(row["status"]),
                row["attempt"],
                row["error_details"],
            )
            for row in rows
        ]

    def cleanup(self, retention_seconds):
        with self._db.connection() as db:
            return db.execute(
                "DELETE FROM notification_log WHERE timestamp<?",
                (time.time() - retention_seconds,),
            ).rowcount
