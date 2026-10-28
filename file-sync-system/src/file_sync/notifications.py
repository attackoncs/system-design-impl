"""Durable device delivery: read-only polling and short acknowledgement writes."""

import math
import time


class EventLog:
    def __init__(self, db, read, transaction, condition, stopping):
        self.db, self.read, self.transaction = db, read, transaction
        self.condition, self.stopping = condition, stopping

    def device(self, user, device):
        if not isinstance(device, str) or not 1 <= len(device) <= 100:
            raise ValueError("invalid device id")
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM devices WHERE id=?", (device,)
            ).fetchone()
            if row is not None and row["user"] != user:
                raise PermissionError("device belongs to another user")
            self.db.execute(
                "INSERT OR IGNORE INTO devices VALUES(?,?,0,0)", (device, user)
            )
            return dict(
                self.db.execute(
                    "SELECT * FROM devices WHERE id=?", (device,)
                ).fetchone()
            )

    def emit(self, kind, space, file=None, revision=None, path=None, users=None):
        """Append inside the caller's write transaction; never sends network I/O."""
        users = (
            users
            if users is not None
            else [
                r[0]
                for r in self.db.execute(
                    "SELECT user FROM members WHERE space=?", (space,)
                )
            ]
        )
        for user in users:
            self.db.execute(
                "INSERT INTO events(user,kind,space,file,revision,path) VALUES(?,?,?,?,?,?)",
                (user, kind, space, file, revision, path),
            )

    def _batch(self, user, device, cursor, limit):
        with self.read():
            row = self.db.execute(
                "SELECT * FROM devices WHERE id=? AND user=?", (device, user)
            ).fetchone()
            if row is None:
                raise PermissionError("device registration required")
            high = self.db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM events"
            ).fetchone()[0]
            if cursor > high:
                raise ValueError("cursor beyond log")
            events = [
                dict(row)
                for row in self.db.execute(
                    "SELECT e.* FROM events e WHERE e.user=? AND e.seq>? "
                    "AND (e.kind='revoke' OR EXISTS (SELECT 1 FROM members m "
                    "WHERE m.space=e.space AND m.user=e.user)) ORDER BY e.seq LIMIT ?",
                    (user, cursor, limit),
                )
            ]
            return {"events": events, "cursor": events[-1]["seq"] if events else high}

    def changes(self, user, device, cursor, wait=0, limit=100, *, cancel=None):
        try:
            valid_wait = (
                type(wait) in (int, float) and math.isfinite(wait) and 0 <= wait <= 25
            )
        except OverflowError:
            valid_wait = False
        if (
            type(cursor) is not int
            or not 0 <= cursor < 2**63
            or not valid_wait
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise ValueError("invalid cursor/wait/limit")
        deadline = time.monotonic() + wait
        while True:
            batch = self._batch(user, device, cursor, limit)
            if (
                batch["events"]
                or time.monotonic() >= deadline
                or self.stopping.is_set()
                or (cancel is not None and cancel.is_set())
            ):
                with self.transaction():
                    # Recheck visibility if sharing changed after the read-only poll.
                    batch = self._batch(user, device, cursor, limit)
                    self.db.execute(
                        "UPDATE devices SET delivered=MAX(delivered,?) WHERE id=? AND user=?",
                        (batch["cursor"], device, user),
                    )
                return batch
            with self.condition:
                # The durable log closes lost-wakeup and cross-process gaps;
                # only local publications can signal this process directly.
                self.condition.wait(min(0.1, max(0, deadline - time.monotonic())))

    def ack(self, user, device, cursor):
        if type(cursor) is not int or not 0 <= cursor < 2**63:
            raise ValueError("invalid cursor")
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM devices WHERE id=? AND user=?", (device, user)
            ).fetchone()
            if row is None:
                raise PermissionError("unknown device")
            if cursor > row["delivered"]:
                raise ValueError("cursor not delivered")
            self.db.execute(
                "UPDATE devices SET cursor=MAX(cursor,?) WHERE id=?", (cursor, device)
            )
