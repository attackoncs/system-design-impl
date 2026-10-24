"""Mute policy stores with fast process-local snapshots.

SQLite mutations commit before the local policy is published. Every running
process owns its snapshot: call reload() after writes made through another
store instance or process. Async callers should offload durable mutations.
"""

from __future__ import annotations

import threading
from typing import Protocol

from news_feed_system.storage import SQLiteDatabase


def _identity(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Mute identities must be non-empty strings")


class MuteStore(Protocol):
    def add(self, user_id: str, muted_id: str) -> None: ...
    def remove(self, user_id: str, muted_id: str) -> None: ...
    def get_muted(self, user_id: str) -> set[str]: ...
    def is_muted(self, user_id: str, author_id: str) -> bool: ...


class InMemoryMuteStore:
    """Thread-safe mute policy for the current process only."""

    def __init__(self) -> None:
        self._mutes: dict[str, set[str]] = {}
        self._lock = threading.RLock()

    def add(self, user_id: str, muted_id: str) -> None:
        _identity(user_id)
        _identity(muted_id)
        with self._lock:
            self._mutes.setdefault(user_id, set()).add(muted_id)

    def remove(self, user_id: str, muted_id: str) -> None:
        _identity(user_id)
        _identity(muted_id)
        with self._lock:
            self._remove_local(user_id, muted_id)

    def _remove_local(self, user_id: str, muted_id: str) -> None:
        current = self._mutes.get(user_id)
        if current is not None:
            current.discard(muted_id)
            if not current:
                del self._mutes[user_id]

    def get_muted(self, user_id: str) -> set[str]:
        with self._lock:
            return set(self._mutes.get(user_id, ()))

    def is_muted(self, user_id: str, author_id: str) -> bool:
        with self._lock:
            return author_id in self._mutes.get(user_id, ())


class SQLiteMuteStore(InMemoryMuteStore):
    """Durable policy loaded into memory on startup, without per-post I/O.

    Reopen or reload after changes written outside this instance. Multiple
    application processes need explicit invalidation/reload coordination.
    """

    def __init__(self, path: str) -> None:
        super().__init__()
        self._database = SQLiteDatabase(path)
        with self._database.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS mutes (
                    user_id TEXT NOT NULL,
                    muted_id TEXT NOT NULL,
                    PRIMARY KEY (user_id, muted_id)
                )
            """)
        self.reload()

    def reload(self) -> None:
        """Replace the local snapshot with one committed database view."""
        with self._lock:
            with self._database.connect() as db:
                rows = db.execute("SELECT user_id, muted_id FROM mutes").fetchall()
            snapshot: dict[str, set[str]] = {}
            for user_id, muted_id in rows:
                snapshot.setdefault(user_id, set()).add(muted_id)
            self._mutes = snapshot

    def add(self, user_id: str, muted_id: str) -> None:
        _identity(user_id)
        _identity(muted_id)
        with self._lock:
            with self._database.connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO mutes(user_id,muted_id) VALUES(?,?)",
                    (user_id, muted_id),
                )
            self._mutes.setdefault(user_id, set()).add(muted_id)

    def remove(self, user_id: str, muted_id: str) -> None:
        _identity(user_id)
        _identity(muted_id)
        with self._lock:
            with self._database.connect() as db:
                db.execute(
                    "DELETE FROM mutes WHERE user_id=? AND muted_id=?",
                    (user_id, muted_id),
                )
            self._remove_local(user_id, muted_id)
