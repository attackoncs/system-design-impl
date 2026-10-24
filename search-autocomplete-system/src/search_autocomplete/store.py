"""Durable analytics and immutable, atomically published serving generations."""

import contextlib
import functools
import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid

import search_autocomplete.trie


class Unavailable(RuntimeError):
    """A snapshot, aggregation, or consistent serving view is unavailable."""


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _window(start, end):
    if not all(_finite_number(value) for value in (start, end)) or not 0 <= start < end:
        raise ValueError("invalid time window")


def _identifier(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"invalid {name}")


def _active(method):
    """Keep close() from invalidating connections during an operation."""

    @functools.wraps(method)
    def run(self, *args, **kwargs):
        with self._condition:
            depth = getattr(self._local, "depth", 0)
            if self._closing and not depth:
                raise RuntimeError("store is closing or closed")
            self._active_operations += 1
            self._local.depth = depth + 1
        try:
            return method(self, *args, **kwargs)
        finally:
            with self._condition:
                self._local.depth -= 1
                self._active_operations -= 1
                self._condition.notify_all()

    return run


class Store:
    """SQLite analytics, filter revisions, aggregations, and serving snapshots.

    File databases use WAL readers for offline aggregation. Trie construction
    and JSON serialization run without the connection lock or a write
    transaction. Separate Store instances coordinate acceptance/publication
    through SQLite transactions, rather than process-local locks.
    """

    def __init__(self, path=":memory:"):
        self.lock = threading.RLock()
        self._condition = threading.Condition(self.lock)
        self._local = threading.local()
        self._active_operations = 0
        self._closing = False
        self._closed = False
        self._blocked_revision = None
        self._blocked_cache = frozenset()
        self._memory = str(path) == ":memory:"
        self._path = (
            f"file:autocomplete-{uuid.uuid4().hex}?mode=memory&cache=shared"
            if self._memory
            else str(path)
        )
        self.db = self._connect()
        try:
            self._initialize()
        except BaseException:
            self.db.close()
            raise

    def _initialize(self):
        # SQLite's journal-mode transition can return BUSY immediately instead
        # of observing busy_timeout when several processes open a new file.
        deadline = time.monotonic() + 30
        while True:
            try:
                self.db.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, query TEXT NOT NULL, at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS event_time ON events(at);
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY, value INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO meta VALUES
                ('active', 0), ('revision', 0), ('build_ticket', 0),
                ('published_ticket', 0);
            INSERT OR IGNORE INTO meta
                SELECT 'event_watermark', COALESCE(MAX(rowid), 0) FROM events;
            CREATE TABLE IF NOT EXISTS blocked (query TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS aggregations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS purged_receipts (
                id TEXT PRIMARY KEY, query_hash TEXT NOT NULL, at REAL NOT NULL
            );
            """
        )

    def _connect(self):
        return sqlite3.connect(
            self._path, uri=self._memory, check_same_thread=False, timeout=30
        )

    @contextlib.contextmanager
    def _reader(self):
        # Shared in-memory databases cannot use WAL. Serialize only their
        # input reads; expensive index construction still happens afterwards.
        guard = self.lock if self._memory else contextlib.nullcontext()
        with guard, contextlib.closing(self._connect()) as reader, reader:
            reader.execute("BEGIN")
            yield reader

    def close(self):
        """Wait for current operations before closing; reject new operations."""
        if getattr(self._local, "depth", 0):
            raise RuntimeError("cannot close the store from an active operation")
        with self._condition:
            self._closing = True
            while self._active_operations:
                self._condition.wait()
            if not self._closed:
                self.db.close()
                self._closed = True

    @_active
    def record(self, event_id, query, at=None):
        """Accept an event once, rejecting a conflicting reuse of its ID.

        Retrying an event whose original timestamp was automatic may omit
        the timestamp again. Explicit timestamps must match exactly. Purged
        events retain a query-hash receipt so retries cannot restore them.
        """
        query = search_autocomplete.trie.normalize(query)
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 200:
            raise ValueError("invalid event ID")
        automatic_time = at is None
        at = time.time() if automatic_time else at
        if not _finite_number(at) or at < 0:
            raise ValueError("invalid timestamp")
        at = float(at)
        query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            old = self.db.execute(
                "SELECT query, at FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            if old:
                same_query = old[0] == query
            else:
                old = self.db.execute(
                    "SELECT query_hash, at FROM purged_receipts WHERE id = ?",
                    (event_id,),
                ).fetchone()
                same_query = old is not None and old[0] == query_hash
            if old:
                if not same_query or (not automatic_time and old[1] != at):
                    raise ValueError("conflicting event ID")
                return False
            self.db.execute(
                "INSERT INTO events(id, query, at) VALUES (?, ?, ?)",
                (event_id, query, at),
            )
            self.db.execute(
                "UPDATE meta SET value = value + 1 WHERE key = 'event_watermark'"
            )
            return True

    @_active
    def view(self):
        """Return active generation, filter revision, and blocked queries atomically."""
        with self.lock, self.db:
            self.db.execute("BEGIN")
            values = dict(self.db.execute("SELECT key, value FROM meta"))
            revision = values["revision"]
            if revision != self._blocked_revision:
                self._blocked_cache = frozenset(
                    row[0] for row in self.db.execute("SELECT query FROM blocked")
                )
                self._blocked_revision = revision
            blocked = self._blocked_cache
        return values["active"], revision, blocked

    @_active
    def revision(self):
        """Read only the current filter revision for a final serving fence."""
        with self.lock:
            return self.db.execute(
                "SELECT value FROM meta WHERE key = 'revision'"
            ).fetchone()[0]

    @_active
    def block(self, query, enabled=True):
        """Set a rule and return the revision committed by this operation."""
        query = search_autocomplete.trie.normalize(query)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            command = (
                "INSERT OR IGNORE INTO blocked(query) VALUES (?)"
                if enabled
                else "DELETE FROM blocked WHERE query = ?"
            )
            if self.db.execute(command, (query,)).rowcount:
                self.db.execute(
                    "UPDATE meta SET value = value + 1 WHERE key = 'revision'"
                )
            return self.db.execute(
                "SELECT value FROM meta WHERE key = 'revision'"
            ).fetchone()[0]

    @_active
    def aggregate(self, start, end):
        """Persist immutable counts for an inclusive/exclusive time window.

        Passing week boundaries implements the chapter's weekly aggregated
        data stage. A consistent read captures its filter revision and event
        watermark; concurrently accepted events enter the next aggregation.
        """
        _window(start, end)
        with self._reader() as reader:
            values = dict(reader.execute("SELECT key, value FROM meta"))
            rows = reader.execute(
                """
                SELECT query, COUNT(*) FROM events
                WHERE at >= ? AND at < ?
                  AND query NOT IN (SELECT query FROM blocked)
                GROUP BY query ORDER BY query
                """,
                (float(start), float(end)),
            ).fetchall()
        data = json.dumps(
            {
                "start": start,
                "end": end,
                "filter_revision": values["revision"],
                "event_watermark": values["event_watermark"],
                "frequencies": rows,
            },
            separators=(",", ":"),
        )
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._check_revision(values["revision"])
            return self.db.execute(
                "INSERT INTO aggregations(data) VALUES (?)", (data,)
            ).lastrowid

    @_active
    def aggregation(self, aggregation_id):
        """Read an immutable materialized aggregation by its ID."""
        _identifier(aggregation_id, "aggregation ID")
        with self.lock:
            row = self.db.execute(
                "SELECT data FROM aggregations WHERE id = ?", (aggregation_id,)
            ).fetchone()
        if row is None:
            raise Unavailable("aggregation unavailable")
        return json.loads(row[0])

    def _check_revision(self, revision):
        actual = self.db.execute(
            "SELECT value FROM meta WHERE key = 'revision'"
        ).fetchone()[0]
        if actual != revision:
            raise Unavailable("filter changed during build; rebuild required")

    def _reserve_ticket(self):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            # MAX(snapshot ID) keeps old databases compatible with this fence.
            self.db.execute(
                """
                UPDATE meta SET value = MAX(
                    value, (SELECT COALESCE(MAX(id), 0) FROM snapshots)
                ) + 1 WHERE key = 'build_ticket'
                """
            )
            return self.db.execute(
                "SELECT value FROM meta WHERE key = 'build_ticket'"
            ).fetchone()[0]

    @_active
    def build(self, start, end, shards=1, *, aggregation_id=None):
        """Prepare and atomically publish a complete immutable generation.

        A supplied aggregation must cover the same time window. Failed,
        filtered-outdated, or superseded builds leave the active snapshot
        unchanged. Generation IDs increase but may contain gaps after failure.
        """
        _window(start, end)
        if (
            not isinstance(shards, int)
            or isinstance(shards, bool)
            or not 1 <= shards <= 256
        ):
            raise ValueError("invalid shard count")
        if aggregation_id is not None:
            _identifier(aggregation_id, "aggregation ID")
        version = self._reserve_ticket()
        if aggregation_id is None:
            aggregation_id = self.aggregate(start, end)
        aggregate = self.aggregation(aggregation_id)
        if (aggregate["start"], aggregate["end"]) != (start, end):
            raise ValueError("aggregation time window does not match")
        rows = aggregate["frequencies"]
        count = min(shards, max(1, len(rows)))
        parts = [
            rows[len(rows) * i // count : len(rows) * (i + 1) // count]
            for i in range(count)
        ]
        for part in parts:
            search_autocomplete.trie.Trie(part)
        data = json.dumps(
            {
                "version": version,
                "start": start,
                "end": end,
                "aggregation_id": aggregation_id,
                "filter_revision": aggregate["filter_revision"],
                "event_watermark": aggregate["event_watermark"],
                "shards": parts,
                "lower": [part[0][0] if part else "" for part in parts],
            },
            separators=(",", ":"),
        )
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._check_revision(aggregate["filter_revision"])
            published = self.db.execute(
                "SELECT value FROM meta WHERE key = 'published_ticket'"
            ).fetchone()[0]
            if published >= version:
                raise Unavailable("snapshot build superseded by a newer publication")
            self.db.execute(
                "INSERT INTO snapshots(id, data) VALUES (?, ?)", (version, data)
            )
            self.db.execute(
                "UPDATE meta SET value = ? WHERE key IN ('active', 'published_ticket')",
                (version,),
            )
        return version

    @_active
    def snapshot(self, version=None):
        """Read one immutable generation, selecting the active one by default."""
        if version is not None:
            _identifier(version, "snapshot generation")
        with self.lock:
            if version is None:
                version = self.db.execute(
                    "SELECT value FROM meta WHERE key = 'active'"
                ).fetchone()[0]
            row = self.db.execute(
                "SELECT data FROM snapshots WHERE id = ?", (version,)
            ).fetchone()
        if row is None:
            raise Unavailable("snapshot generation unavailable")
        return json.loads(row[0])

    @_active
    def purge_blocked(self):
        """Delete blocked source rows and outdated aggregations atomically.

        Retained serving snapshots remain immutable and obey live filters.
        Schedule snapshot retention separately when historical copies should
        also be removed. Receipts retain hashes for event retry deduplication.
        """
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                "SELECT id, query, at FROM events WHERE query IN (SELECT query FROM blocked)"
            ).fetchall()
            self.db.executemany(
                "INSERT OR IGNORE INTO purged_receipts(id, query_hash, at) VALUES (?, ?, ?)",
                [
                    (event_id, hashlib.sha256(query.encode("utf-8")).hexdigest(), at)
                    for event_id, query, at in rows
                ],
            )
            self.db.execute(
                "DELETE FROM events WHERE query IN (SELECT query FROM blocked)"
            )
            if rows:
                # Aggregations are immutable too: remove obsolete artifacts
                # rather than rewriting IDs that a worker might have loaded.
                self.db.execute("DELETE FROM aggregations")
            return len(rows)
