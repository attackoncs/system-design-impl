"""Shared write lease for promoting a caught-up read replica.

The chapter promotes a slave when the master is offline. This fence is the
lab form of that step: the current writer publishes its change cursor and
renews a short lease. A replica may take the next epoch only after the lease
expires, and only when its own cursor is at least the published one. A lagging
replica is refused so the last acknowledged history is not truncated.

The fence does not copy sessions, idempotency records, or outbox jobs. Those
stay on the original primary. Profile writes on a demoted holder fail closed.
"""

from contextlib import contextmanager
import sqlite3
import threading

from .common import Error, identifier, integer
from .contracts import timestamp


class Fence:
    def __init__(self, path, holder, clock):
        self.holder = identifier(holder)
        self.clock = clock
        self._thread = threading.Lock()
        self._path = path
        db = self._connect()
        try:
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

    def _connect(self):
        db = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @contextmanager
    def _transaction(self):
        with self._thread:
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                try:
                    yield db
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
                db.execute("COMMIT")
            finally:
                db.close()

    def _exclusive(self, mutate):
        with self._transaction() as db:
            return mutate(db, timestamp(self.clock()))

    def _held(self, db, now):
        """Return the current row, failing closed unless this holder's lease is live."""
        row = _row(db)
        if row[1] != self.holder or row[3] <= now:
            raise Error(403, "database lease is not held")
        return row

    def _take(self, db, epoch, cursor, now, lease_seconds):
        expires = now + lease_seconds
        db.execute(
            "UPDATE fence SET epoch=?, holder=?, cursor=?, expires=? WHERE id=1",
            (epoch, self.holder, cursor, expires),
        )
        return {
            "epoch": epoch,
            "holder": self.holder,
            "cursor": cursor,
            "expires": expires,
        }

    @contextmanager
    def writing(self, lease_seconds=2):
        """Exclude promotion until a profile transaction commits and publishes."""
        lease_seconds = integer(lease_seconds, 1, 60)
        with self._transaction() as db:
            now = timestamp(self.clock())
            self._held(db, now)
            db.execute("UPDATE fence SET expires=? WHERE id=1", (now + lease_seconds,))

            def publish(cursor):
                cursor = integer(cursor, 0, 1000)
                now = timestamp(self.clock())
                _ahead(cursor, self._held(db, now)[2])
                db.execute(
                    "UPDATE fence SET cursor=?,expires=? WHERE id=1",
                    (cursor, now + lease_seconds),
                )

            yield publish

    def status(self):
        db = self._connect()
        try:
            epoch, holder, cursor, expires = _row(db)
        finally:
            db.close()
        return {"epoch": epoch, "holder": holder, "cursor": cursor, "expires": expires}

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
            _ahead(cursor, published)
            new_epoch = 1 if holder == "" else epoch
            return self._take(db, new_epoch, cursor, now, lease_seconds)

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
            return self._take(db, new_epoch, cursor, now, lease_seconds)

        return self._exclusive(mutate)

    def assert_held(self, lease_seconds=2):
        """Renew a held lease; profile mutations must use writing() for isolation."""
        lease_seconds = integer(lease_seconds, 1, 60)

        def mutate(db, now):
            epoch = self._held(db, now)[0]
            db.execute("UPDATE fence SET expires=? WHERE id=1", (now + lease_seconds,))
            return epoch

        return self._exclusive(mutate)

    def publish(self, cursor):
        """Standalone cursor publication; writing() supplies a protected publisher."""
        cursor = integer(cursor, 0, 1000)

        def mutate(db, now):
            _ahead(cursor, self._held(db, now)[2])
            db.execute("UPDATE fence SET cursor=? WHERE id=1", (cursor,))
            return cursor

        return self._exclusive(mutate)


def _ahead(cursor, published):
    if cursor < published:
        raise Error(409, "published cursor is ahead of this database")


def _row(db):
    row = db.execute(
        "SELECT epoch, holder, cursor, expires FROM fence WHERE id=1"
    ).fetchone()
    return row["epoch"], row["holder"], row["cursor"], row["expires"]
