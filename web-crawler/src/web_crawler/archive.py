"""Durable HTML snapshots implemented as an asynchronous content hook."""

import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timezone


class SQLiteArchive:
    def __init__(self, path):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("archive requires a file path")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, url TEXT, fetched_at TEXT, status INTEGER, html TEXT)"
            )
            db.commit()

    async def __call__(self, *, url, status, headers, content, links):
        await asyncio.to_thread(self._save, url, status, content)

    def _save(self, url, status, content):
        with closing(sqlite3.connect(self.path, timeout=30)) as db:
            db.execute(
                "INSERT INTO snapshots(url,fetched_at,status,html) VALUES(?,?,?,?)",
                (url, datetime.now(timezone.utc).isoformat(), status, content),
            )
            db.commit()
