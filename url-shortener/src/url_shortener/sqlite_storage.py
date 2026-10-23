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

    def get_or_create(self, long_url, factory):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._local.connection = db
            try:
                mapping = self.get_by_long_url(long_url)
                if mapping is None:
                    mapping = factory()
                    self.save(mapping)
                db.execute("COMMIT")
                return mapping
            except BaseException:
                db.execute("ROLLBACK")
                raise
            finally:
                del self._local.connection

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
