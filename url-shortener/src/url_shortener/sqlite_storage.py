"""Transactional persistence shared by independent shortener instances."""

import datetime
import sqlite3
import threading
from contextlib import contextmanager
from .storage import StorageBackend
from .models import URLMapping, RedirectType


class SQLiteStorage(StorageBackend):
    def __init__(self, path):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("use a file path for persistent storage")
        self._local = threading.local()
        with self._connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS urls (id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, url TEXT UNIQUE NOT NULL, redirect INTEGER NOT NULL, created TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS id_sequence "
                "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), value INTEGER NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO id_sequence VALUES "
                "(1, (SELECT COALESCE(MAX(id), 0) FROM urls))"
            )

    @contextmanager
    def _connection(self):
        current = getattr(self._local, "connection", None)
        if current is not None:
            yield current
            return
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        """Reuse the mapping transaction when its factory allocates an ID."""
        with self._connection() as db:
            if getattr(self._local, "connection", None) is db:
                yield db
                return
            db.execute("BEGIN IMMEDIATE")
            self._local.connection = db
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            finally:
                del self._local.connection

    def get_or_create(self, long_url, factory):
        # Mappings are immutable: an existing row needs no writer reservation.
        mapping = self.get_by_long_url(long_url)
        if mapping is not None:
            return mapping
        with self._transaction():
            # Another process may have created it before BEGIN IMMEDIATE.
            mapping = self.get_by_long_url(long_url)
            if mapping is None:
                mapping = factory()
                self.save(mapping)
            return mapping

    def next_id(self) -> int:
        """Allocate a durable ID for Base62Strategy(id_generator=this_storage).

        All writers must use this allocator, with the same storage object as
        their mapping factory. This is a single SQLite file, not a distributed
        allocator or a migration from arbitrary external IDs.
        """
        with self._transaction() as db:
            current = db.execute("SELECT value FROM id_sequence WHERE singleton=1").fetchone()[0]
            if current >= 2**63 - 1:
                raise OverflowError("SQLite ID sequence exhausted")
            next_id = current + 1
            db.execute("UPDATE id_sequence SET value=? WHERE singleton=1", (next_id,))
            return next_id

    def save(self, mapping):
        with self._connection() as db:
            try:
                db.execute(
                    "INSERT INTO urls(code,url,redirect,created) VALUES(?,?,?,?)",
                    (
                        mapping.short_code,
                        mapping.long_url,
                        int(mapping.redirect_type),
                        mapping.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                if self.get_by_short_code(mapping.short_code) != mapping:
                    raise ValueError(
                        "mapping conflicts with an existing immutable mapping"
                    ) from error

    def _lookup(self, column, value):
        with self._connection() as db:
            row = db.execute(
                f"SELECT code,url,redirect,created FROM urls WHERE {column}=?", (value,)
            ).fetchone()
        return (
            None
            if row is None
            else URLMapping(
                row[0],
                row[1],
                RedirectType(row[2]),
                datetime.datetime.fromisoformat(row[3]),
            )
        )

    def get_by_short_code(self, short_code):
        return self._lookup("code", short_code)

    def get_by_long_url(self, long_url):
        return self._lookup("url", long_url)

    def exists(self, short_code):
        return self.get_by_short_code(short_code) is not None
