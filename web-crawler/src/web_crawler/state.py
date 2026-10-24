"""Single-owner durable frontier, completion journal and HTML snapshots.

SQLite holds the full queue; only a bounded window of eligible host heads is
read into memory. A process lock prevents two local schedulers from sharing an
identity or resetting one another's in-flight work during recovery.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import os
import math
import time

from .models import CrawlResult, CrawlStatus, FrontierEntry, Priority
from .parser import normalize_url


class SQLiteCrawlState:
    def __init__(
        self,
        path,
        *,
        per_host_delay=1.0,
        recrawl_interval=None,
        max_retries=2,
        retry_delay=1.0,
        window_size=128,
        clock=time.time,
    ):
        if str(path) == ":memory:":
            raise ValueError("durable state requires a file path")
        for name, value in (("window_size", window_size),):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer")
        for name, value in (
            ("per_host_delay", per_host_delay),
            ("retry_delay", retry_delay),
        ):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if recrawl_interval is not None and (
            type(recrawl_interval) not in (int, float)
            or not math.isfinite(recrawl_interval)
            or recrawl_interval <= 0
        ):
            raise ValueError("recrawl_interval must be finite and positive")
        self.path = Path(path).resolve()
        self.delay = per_host_delay
        self.interval = recrawl_interval
        self.max_retries, self.retry_delay = max_retries, retry_delay
        self.window_size, self.clock = window_size, clock
        self._lock_file = None
        self._active = set()
        self._next_host = {}

    @contextmanager
    def _db(self):
        if self._lock_file is None:
            raise RuntimeError("crawl state is not open")
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            with db:
                yield db

    def open(self):
        if self._lock_file is not None:
            raise RuntimeError("crawl state already open")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(self.path) + ".lock", "a+b")
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise RuntimeError(
                "crawl state is already owned by another crawler"
            ) from error
        self._lock_file = handle
        try:
            with self._db() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS jobs (
                        url TEXT PRIMARY KEY, host TEXT NOT NULL,
                        priority INTEGER NOT NULL, depth INTEGER NOT NULL,
                        state TEXT NOT NULL, due REAL NOT NULL DEFAULT 0,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        fingerprint TEXT, unchanged INTEGER NOT NULL DEFAULT 0,
                        last_status TEXT, last_error TEXT);
                    CREATE INDEX IF NOT EXISTS ready_jobs ON jobs(state,due,priority);
                    CREATE TABLE IF NOT EXISTS fingerprints (hash TEXT PRIMARY KEY);
                    CREATE TABLE IF NOT EXISTS hosts (host TEXT PRIMARY KEY, next_ready REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS snapshots (
                        id INTEGER PRIMARY KEY, url TEXT NOT NULL,
                        fetched_at REAL NOT NULL, status INTEGER, html TEXT NOT NULL);
                """)
                db.execute(
                    "UPDATE jobs SET state='pending',due=0 WHERE state='inflight'"
                )
                if self.interval is not None:
                    db.execute(
                        "UPDATE jobs SET state='pending',attempts=0 WHERE state='done' AND due>0 AND due<=?",
                        (self.clock(),),
                    )
                self._next_host = dict(db.execute("SELECT host,next_ready FROM hosts"))
            self._active.clear()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._lock_file is not None:
            self._lock_file.close()  # OS releases the lock even after a crash.
            self._lock_file = None
        self._active.clear()

    def put(self, entry):
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO jobs(url,host,priority,depth,state) VALUES(?,?,?,?,'pending')",
                (
                    normalize_url(entry.url),
                    entry.host,
                    entry.priority.value,
                    entry.depth,
                ),
            )

    def get(self, *, reserve=True):
        if not reserve:
            raise ValueError("durable frontier requires reservation and completion")
        now = self.clock()
        with self._db() as db:
            # One candidate per eligible host avoids a hot host hiding other hosts
            # beyond the bounded read window. SQLite keeps the full queue on disk.
            excluded = tuple(self._active)
            active_filter = (
                ""
                if not excluded
                else " AND j.host NOT IN (" + ",".join("?" for _ in excluded) + ")"
            )
            rows = db.execute(
                """
                SELECT url,host,priority,depth FROM (
                    SELECT j.url,j.host,j.priority,j.depth,
                           ROW_NUMBER() OVER (PARTITION BY j.host ORDER BY j.priority,j.rowid) AS rank
                    FROM jobs j LEFT JOIN hosts h ON h.host=j.host
                    WHERE j.state='pending' AND j.due<=? AND COALESCE(h.next_ready,0)<=?
                """
                + active_filter
                + """ )
                WHERE rank=1 ORDER BY priority,url LIMIT ?
            """,
                (now, now, *excluded, self.window_size),
            ).fetchall()
            for url, host, priority, depth in rows:
                if host in self._active or self._next_host.get(host, 0) > now:
                    continue
                db.execute(
                    "UPDATE jobs SET state='inflight',attempts=attempts+1 WHERE url=?",
                    (url,),
                )
                self._active.add(host)
                return FrontierEntry(url, Priority(priority), depth, host)
        return None

    def is_empty(self):
        # Future retries keep a run alive. Future recrawls wait for the next run.
        with self._db() as db:
            return (
                db.execute(
                    "SELECT 1 FROM jobs WHERE state IN ('pending','inflight') LIMIT 1"
                ).fetchone()
                is None
            )

    def content_seen(self, fingerprint):
        with self._db() as db:
            return (
                db.execute(
                    "SELECT 1 FROM fingerprints WHERE hash=?", (fingerprint,)
                ).fetchone()
                is not None
            )

    def retry(self, entry):
        ready = self.clock() + self.delay
        with self._db() as db:
            db.execute(
                "UPDATE jobs SET state='pending',due=0 WHERE url=? AND state='inflight'",
                (normalize_url(entry.url),),
            )
            db.execute(
                "INSERT OR REPLACE INTO hosts(host,next_ready) VALUES(?,?)",
                (entry.host, ready),
            )
        self._release_local(entry, ready)

    def _release_local(self, entry, ready):
        self._next_host[entry.host] = ready
        self._active.discard(entry.host)

    def finish(self, entry, result: CrawlResult, html=None):
        """Commit snapshot, fingerprint and acknowledgement in one transaction."""
        now = self.clock()
        with self._db() as db:
            old = db.execute(
                "SELECT fingerprint,unchanged,attempts FROM jobs WHERE url=?",
                (normalize_url(entry.url),),
            ).fetchone()
            previous, unchanged, attempts = old
            state, due = "done", 0
            if result.status == CrawlStatus.ERROR and attempts <= self.max_retries:
                state, due = (
                    "pending",
                    now + self.retry_delay * 2 ** min(attempts - 1, 20),
                )
            elif self.interval is not None and result.status in (
                CrawlStatus.SUCCESS,
                CrawlStatus.DUPLICATE,
            ):
                unchanged = (
                    min(unchanged + 1, 4)
                    if previous == result.content_fingerprint
                    else 0
                )
                due = now + self.interval * 2**unchanged
            fingerprint = result.content_fingerprint or previous
            priority = Priority.HIGH.value if unchanged == 0 else entry.priority.value
            db.execute(
                "UPDATE jobs SET state=?,due=?,fingerprint=?,unchanged=?,priority=?,last_status=?,last_error=? WHERE url=?",
                (
                    state,
                    due,
                    fingerprint,
                    unchanged,
                    priority,
                    result.status.value,
                    result.error_message,
                    normalize_url(entry.url),
                ),
            )
            if result.status == CrawlStatus.SUCCESS and html is not None:
                db.execute(
                    "INSERT INTO snapshots(url,fetched_at,status,html) VALUES(?,?,?,?)",
                    (entry.url, now, result.http_status, html),
                )
                db.execute(
                    "INSERT OR IGNORE INTO fingerprints(hash) VALUES(?)",
                    (result.content_fingerprint,),
                )
            db.execute(
                "INSERT OR REPLACE INTO hosts(host,next_ready) VALUES(?,?)",
                (entry.host, now + self.delay),
            )
        self._release_local(entry, now + self.delay)
