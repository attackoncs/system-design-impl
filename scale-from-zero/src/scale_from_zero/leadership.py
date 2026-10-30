"""Shared write lease for promoting a caught-up read replica.

The chapter promotes a slave when the master is offline. This fence is the
lab form of that step: the current writer publishes its change cursor and
renews a short lease. A replica may take the next epoch only after the lease
expires, and only when its own cursor is at least the published one. A lagging
replica is refused so the last acknowledged history is not truncated.

The fence does not copy sessions, idempotency records, or outbox jobs. Those
stay on the original primary. Profile writes on a demoted holder fail closed.
"""

import sqlite3
import threading

from .common import Error, identifier, integer
from .contracts import timestamp


class Fence:
    def __init__(self, path, holder, clock):
        self.holder = identifier(holder)
        self.clock = clock
        self._thread = threading.Lock()
        db = sqlite3.connect(path, timeout=5, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute(
                """CREATE TABLE IF NOT EXISTS fence(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    epoch INTEGER NOT NULL,
                    holder TEXT NOT NULL,
                    cursor INTEGER NOT NULL,
                    expires REAL NOT NULL
                )"""
            )
            db.execute("INSERT OR IGNORE INTO fence VALUES(1,0,'',-1,0)")
        finally:
            db.close()
        self._path = path

    def _connect(self):
        db = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _exclusive(self, mutate):
        with self._thread:
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                try:
                    result = mutate(db, timestamp(self.clock()))
                except Exception:
                    db.execute("ROLLBACK")
                    raise
                db.execute("COMMIT")
                return result
            finally:
                db.close()

    def status(self):
        db = self._connect()
        try:
            row = db.execute(
                "SELECT epoch, holder, cursor, expires FROM fence WHERE id=1"
            ).fetchone()
        finally:
            db.close()
        return {
            "epoch": row["epoch"],
            "holder": row["holder"],
            "cursor": row["cursor"],
            "expires": row["expires"],
        }

    def holds(self):
        row = self.status()
        return row["holder"] == self.holder and row["expires"] > timestamp(self.clock())

    def heartbeat(self, cursor, lease_seconds=2):
        """Renew the lease. An expired foreign holder must be promoted explicitly."""
        cursor = integer(cursor, 0, 1000)
        lease_seconds = integer(lease_seconds, 1, 60)

        def mutate(db, now):
            epoch, holder, published, expires = _row(db)
            if holder not in ("", self.holder):
                if expires > now:
                    raise Error(409, "another writer holds the lease")
                raise Error(409, "lease expired; promote the replica explicitly")
            if cursor < published:
                raise Error(409, "published cursor is ahead of this database")
            new_epoch = 1 if holder == "" else epoch
            expires_at = now + lease_seconds
            db.execute(
                "UPDATE fence SET epoch=?, holder=?, cursor=?, expires=? WHERE id=1",
                (new_epoch, self.holder, cursor, expires_at),
            )
            return {
                "epoch": new_epoch,
                "holder": self.holder,
                "cursor": cursor,
                "expires": expires_at,
            }

        return self._exclusive(mutate)

    def promote(self, cursor, lease_seconds=2):
        """Take the next epoch after the current lease expires, without rewinding history."""
        cursor = integer(cursor, 0, 1000)
        lease_seconds = integer(lease_seconds, 1, 60)

        def mutate(db, now):
            epoch, holder, published, expires = _row(db)
            if cursor < published:
                raise Error(
                    409,
                    "replica is behind the published cursor; recover missing changes before promotion",
                )
            if holder not in ("", self.holder) and expires > now:
                raise Error(409, "primary lease is still valid")
            new_epoch = epoch if holder == self.holder and expires > now else epoch + 1
            if holder == "" and epoch == 0:
                new_epoch = 1
            expires_at = now + lease_seconds
            db.execute(
                "UPDATE fence SET epoch=?, holder=?, cursor=?, expires=? WHERE id=1",
                (new_epoch, self.holder, cursor, expires_at),
            )
            return {
                "epoch": new_epoch,
                "holder": self.holder,
                "cursor": cursor,
                "expires": expires_at,
            }

        return self._exclusive(mutate)

    def assert_held(self, lease_seconds=2):
        """Extend the lease or refuse the write. Call this before a profile commit."""
        lease_seconds = integer(lease_seconds, 1, 60)

        def mutate(db, now):
            epoch, holder, _published, expires = _row(db)
            if holder != self.holder or expires <= now:
                raise Error(403, "database lease is not held")
            expires_at = now + lease_seconds
            db.execute("UPDATE fence SET expires=? WHERE id=1", (expires_at,))
            return epoch

        return self._exclusive(mutate)

    def publish(self, cursor):
        """Advance the acknowledged cursor after a committed profile change."""
        cursor = integer(cursor, 0, 1000)

        def mutate(db, now):
            _epoch, holder, published, expires = _row(db)
            if holder != self.holder or expires <= now:
                raise Error(403, "database lease is not held")
            if cursor < published:
                raise Error(409, "published cursor is ahead of this database")
            db.execute("UPDATE fence SET cursor=? WHERE id=1", (cursor,))
            return cursor

        return self._exclusive(mutate)


def _row(db):
    row = db.execute(
        "SELECT epoch, holder, cursor, expires FROM fence WHERE id=1"
    ).fetchone()
    return row["epoch"], row["holder"], row["cursor"], row["expires"]
