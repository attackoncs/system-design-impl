"""Append-only analytics and atomically published SQLite snapshot
generations.
"""

import json
import math
import sqlite3
import threading
import time

import search_autocomplete.trie


class Unavailable(RuntimeError):
    """A requested snapshot or consistent serving view is unavailable."""


class Store:
    """Thread-safe SQLite analytics, filter revisions, and snapshot storage."""

    def __init__(self, path=":memory:"):
        """Open the SQLite database at path and initialize its schema."""
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            "\n"
            "            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY"
            " KEY, query TEXT NOT NULL, at REAL NOT NULL);\n"
            "            CREATE INDEX IF NOT EXISTS event_time ON events(a"
            "t);\n"
            "            CREATE TABLE IF NOT EXISTS snapshots(id INTEGER P"
            "RIMARY KEY, data TEXT NOT NULL);\n"
            "            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY "
            "KEY, value INTEGER NOT NULL);\n"
            "            INSERT OR IGNORE INTO meta VALUES('active',0),('r"
            "evision',0);\n"
            "            CREATE TABLE IF NOT EXISTS blocked(query TEXT PRI"
            "MARY KEY);\n"
            "        "
        )

    def close(self):
        """Close the database after pending operations release the lock."""
        with self.lock:
            self.db.close()

    def record(self, event_id, query, at=None):
        """Persist a search event exactly once for its caller-provided ID.

        Args:
            event_id: Nonempty idempotency key, at most 200 characters.
            query: Query to normalize and record.
            at: Nonnegative epoch seconds; None uses the current time.

        Returns:
            True for a new event, False for an identical retry.

        Raises:
            ValueError: Input is invalid or the ID conflicts with stored data.
        """
        query = search_autocomplete.trie.normalize(query)
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 200:
            raise ValueError("invalid event ID")
        automatic_time = at is None
        at = time.time() if automatic_time else at
        if (
            isinstance(at, bool)
            or not isinstance(at, (float, int))
            or not math.isfinite(at)
            or at < 0
        ):
            raise ValueError("invalid timestamp")
        with self.lock, self.db:
            old = self.db.execute(
                "SELECT query,at FROM events WHERE id=?", (event_id,)
            ).fetchone()
            if old:
                if old[0] != query or (not automatic_time and old[1] != at):
                    raise ValueError("conflicting event ID")
                return False
            self.db.execute(
                "INSERT INTO events VALUES(?,?,?)", (event_id, query, at)
            )
            return True

    def view(self):
        """Return (active generation, filter revision, blocked queries)
        atomically.
        """
        with self.lock:
            # One read transaction also fences concurrent writers from other
            # processes.
            with self.db:
                self.db.execute("BEGIN")
                values = dict(self.db.execute("SELECT key,value FROM meta"))
                blocked = frozenset(
                    row[0]
                    for row in self.db.execute("SELECT query FROM blocked")
                )
            return values["active"], values["revision"], blocked

    def block(self, query, enabled=True):
        """Set a query's blocked status and return the current filter
        revision.
        """
        query = search_autocomplete.trie.normalize(query)
        with self.lock, self.db:
            command = (
                "INSERT OR IGNORE INTO blocked VALUES(?)"
                if enabled
                else "DELETE FROM blocked WHERE query=?"
            )
            changed = self.db.execute(command, (query,)).rowcount
            if changed:
                self.db.execute(
                    "UPDATE meta SET value=value+1 WHERE key='revision'"
                )
        return self.view()[1]

    def build(self, start, end, shards=1):
        """Publish a complete snapshot built from an event time window.

        Args:
            start: Inclusive lower bound in nonnegative epoch seconds.
            end: Exclusive upper bound, greater than start.
            shards: Requested lexical partitions, between 1 and 256.

        Returns:
            The new snapshot generation ID; publication commits atomically.

        Raises:
            ValueError: The time window or shard count is invalid.
        """
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not math.isfinite(start)
            or not math.isfinite(end)
            or not 0 <= start < end
            or (not isinstance(shards, int) or isinstance(shards, bool))
            or not 1 <= shards <= 256
        ):
            raise ValueError("invalid time window or shard count")
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                "SELECT query,COUNT(*) FROM events\n"
                "                WHERE at>=? AND at<? AND query NOT IN (SE"
                "LECT query FROM blocked)\n"
                "                GROUP BY query ORDER BY query",
                (start, end),
            ).fetchall()
            count = min(shards, max(1, len(rows)))
            parts = [
                rows[len(rows) * i // count : len(rows) * (i + 1) // count]
                for i in range(count)
            ]
            for part in parts:
                search_autocomplete.trie.Trie(
                    part
                )  # Validation/build failure must not publish any generation.
            version = self.db.execute(
                "SELECT COALESCE(MAX(id),0)+1 FROM snapshots"
            ).fetchone()[0]
            data = {
                "version": version,
                "start": start,
                "end": end,
                "shards": parts,
                "lower": [part[0][0] if part else "" for part in parts],
            }
            self.db.execute(
                "INSERT INTO snapshots VALUES(?,?)", (version, json.dumps(data))
            )
            self.db.execute(
                "UPDATE meta SET value=? WHERE key='active'", (version,)
            )
            return version

    def snapshot(self, version=None):
        """Return the requested generation, or the active one when omitted.

        Raises:
            Unavailable: The requested generation has not been published.
        """
        with self.lock:
            if version is None:
                version = self.db.execute(
                    "SELECT value FROM meta WHERE key='active'"
                ).fetchone()[0]
            row = self.db.execute(
                "SELECT data FROM snapshots WHERE id=?", (version,)
            ).fetchone()
        if row is None:
            raise Unavailable("snapshot generation unavailable")
        return json.loads(row[0])
