"""Durable device delivery: read-only polling and short acknowledgement writes.

Retention: the log is bounded per space by age and/or row count. Pruning removes
only an oldest prefix of a space's events (an indexed range delete) and raises
that space's floor in the same transaction, so "seq <= floor" is exactly the
pruned set. A device whose cursor is below the floor of any of its spaces is
told to re-snapshot instead of receiving a batch with a silent gap.
"""

import math
import time


class EventLog:
    def __init__(
        self, db, read, transaction, condition, stopping, clock, max_age, max_per_space
    ):
        self.db, self.read, self.transaction = db, read, transaction
        self.condition, self.stopping = condition, stopping
        self.clock, self.max_age, self.max_per_space = clock, max_age, max_per_space

    def high(self):
        """Highest sequence ever assigned; AUTOINCREMENT survives pruning."""
        row = self.db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='events'"
        ).fetchone()
        return row[0] if row else 0

    def stale(self, user, cursor):
        """True when events after `cursor` in one of the user's spaces were pruned."""
        return (
            self.db.execute(
                "SELECT 1 FROM members m JOIN event_spaces s ON s.space=m.space "
                "WHERE m.user=? AND s.floor>? LIMIT 1",
                (user, cursor),
            ).fetchone()
            is not None
        )

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
        now = self.clock()
        for user in users:
            self.db.execute(
                "INSERT INTO events(user,kind,space,file,revision,path,created) VALUES(?,?,?,?,?,?,?)",
                (user, kind, space, file, revision, path, now),
            )
        self.db.execute(
            "INSERT INTO event_spaces(space,retained) VALUES(?,?) "
            "ON CONFLICT(space) DO UPDATE SET retained=retained+excluded.retained",
            (space, len(users)),
        )
        # Amortized retention: each append may remove a bounded oldest prefix,
        # at least as many rows as it added, so an active space converges.
        self.retain(space, max(64, 2 * len(users)))

    def retain(self, space, budget):
        """Prune at most `budget` expired/excess oldest events of one space.

        Runs inside the caller's write transaction. Reads the oldest rows via
        the (space, seq) index and deletes a contiguous prefix by range.
        """
        state = self.db.execute(
            "SELECT retained FROM event_spaces WHERE space=?", (space,)
        ).fetchone()
        if state is None:
            return 0
        excess = (
            state["retained"] - self.max_per_space
            if self.max_per_space is not None
            else 0
        )
        cutoff = self.clock() - self.max_age if self.max_age is not None else None
        if excess <= 0 and cutoff is None:
            return 0
        last, count = None, 0
        for seq, created in self.db.execute(
            "SELECT seq,created FROM events WHERE space=? ORDER BY seq LIMIT ?",
            (space, budget),
        ).fetchall():
            # Stop at the first row that is neither excess nor expired, keeping
            # the deleted set a prefix even if the clock stepped backwards.
            if count < excess or (
                cutoff is not None and created is not None and created < cutoff
            ):
                last, count = seq, count + 1
            else:
                break
        if last is None:
            return 0
        self.db.execute("DELETE FROM events WHERE space=? AND seq<=?", (space, last))
        self.db.execute(
            "UPDATE event_spaces SET floor=MAX(floor,?),retained=retained-? WHERE space=?",
            (last, count, space),
        )
        return count

    def _batch(self, user, device, cursor, limit):
        with self.read():
            row = self.db.execute(
                "SELECT * FROM devices WHERE id=? AND user=?", (device, user)
            ).fetchone()
            if row is None:
                raise PermissionError("device registration required")
            high = self.high()
            if cursor > high:
                raise ValueError("cursor beyond log")
            if self.stale(user, cursor):
                # Same read snapshot as the floors: no gap can slip through.
                return {"events": [], "cursor": cursor, "resnapshot": True}
            events = [
                # `created` is retention bookkeeping, not part of the wire format.
                {key: row[key] for key in row.keys() if key != "created"}
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
                or batch.get("resnapshot")
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
