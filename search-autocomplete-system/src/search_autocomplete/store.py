"""Append-only analytics and atomically published SQLite snapshot generations."""
import json
import math
import sqlite3
import threading
import time

from .trie import Trie, normalize


class Unavailable(RuntimeError):
    pass


class Store:
    def __init__(self, path=":memory:"):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, query TEXT NOT NULL, at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS event_time ON events(at);
            CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
            INSERT OR IGNORE INTO meta VALUES('active',0),('revision',0);
            CREATE TABLE IF NOT EXISTS blocked(query TEXT PRIMARY KEY);
        """)

    def close(self):
        with self.lock:
            self.db.close()

    def record(self, event_id, query, at=None):
        query = normalize(query)
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 200:
            raise ValueError("invalid event ID")
        automatic_time = at is None
        at = time.time() if automatic_time else at
        if isinstance(at, bool) or not isinstance(at, (float, int)) or not math.isfinite(at) or at < 0:
            raise ValueError("invalid timestamp")
        with self.lock, self.db:
            old = self.db.execute("SELECT query,at FROM events WHERE id=?", (event_id,)).fetchone()
            if old:
                if old[0] != query or (not automatic_time and old[1] != at):
                    raise ValueError("conflicting event ID")
                return False
            self.db.execute("INSERT INTO events VALUES(?,?,?)", (event_id, query, at))
            return True

    def view(self):
        with self.lock:
            # One read transaction also fences concurrent writers from other processes.
            with self.db:
                self.db.execute("BEGIN")
                values = dict(self.db.execute("SELECT key,value FROM meta"))
                blocked = frozenset(row[0] for row in self.db.execute("SELECT query FROM blocked"))
            return values["active"], values["revision"], blocked

    def block(self, query, enabled=True):
        query = normalize(query)
        with self.lock, self.db:
            command = "INSERT OR IGNORE INTO blocked VALUES(?)" if enabled else "DELETE FROM blocked WHERE query=?"
            changed = self.db.execute(command, (query,)).rowcount
            if changed:
                self.db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")
        return self.view()[1]

    def build(self, start, end, shards=1):
        if (isinstance(start, bool) or isinstance(end, bool) or
                not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or
                not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end or
                type(shards) is not int or not 1 <= shards <= 256):
            raise ValueError("invalid time window or shard count")
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute("""SELECT query,COUNT(*) FROM events
                WHERE at>=? AND at<? AND query NOT IN (SELECT query FROM blocked)
                GROUP BY query ORDER BY query""", (start, end)).fetchall()
            count = min(shards, max(1, len(rows)))
            parts = [rows[len(rows) * i // count:len(rows) * (i + 1) // count] for i in range(count)]
            for part in parts:
                Trie(part)  # Validation/build failure must not publish any generation.
            version = self.db.execute("SELECT COALESCE(MAX(id),0)+1 FROM snapshots").fetchone()[0]
            data = {"version": version, "start": start, "end": end, "shards": parts,
                    "lower": [part[0][0] if part else "" for part in parts]}
            self.db.execute("INSERT INTO snapshots VALUES(?,?)", (version, json.dumps(data)))
            self.db.execute("UPDATE meta SET value=? WHERE key='active'", (version,))
            return version

    def snapshot(self, version=None):
        with self.lock:
            if version is None:
                version = self.db.execute("SELECT value FROM meta WHERE key='active'").fetchone()[0]
            row = self.db.execute("SELECT data FROM snapshots WHERE id=?", (version,)).fetchone()
        if row is None:
            raise Unavailable("snapshot generation unavailable")
        return json.loads(row[0])
