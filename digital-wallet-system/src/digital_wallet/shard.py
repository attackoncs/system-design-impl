import json
import os
import tempfile
import threading
import time
from pathlib import Path

from .common import (
    GENESIS_HASH,
    Error,
    Fence,
    checksum,
    encode,
    entry,
    identifier,
    integer,
    shard_for,
    verify,
)


from .contracts import intent as intent
from .projection import apply_event, apply_events
from .projection import reduce_event as reduce_event


class Shard(Fence):
    """One account shard: immutable hash-chained events plus a projection.

    Recovery follows the book's snapshot model: load the latest checksummed
    snapshot, confirm the log still contains its head, and replay only the
    verified tail. ``audit`` remains the full-history check: it replays from
    genesis and compares both the projection and the snapshot.
    """

    def __init__(self, path, index, count, accounts, epoch=1, leader="coordinator"):
        super().__init__(path)
        integer(count, 1, 100)
        integer(index, 0, count - 1)
        self.index, self.count = index, count
        self.snapshot_path = Path(str(self.path) + ".snapshot.json")
        self.snapshot_lock = threading.RLock()
        self.init_fence(epoch, leader)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,body TEXT);
        CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable event'); END;
        CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable event'); END;
        CREATE TABLE IF NOT EXISTS accounts(account TEXT PRIMARY KEY,available INTEGER NOT NULL,held INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS parts(id TEXT PRIMARY KEY,body TEXT NOT NULL);
        DROP TABLE IF EXISTS projection;
        """)
        opening = {
            key: integer(value)
            for key, value in accounts.items()
            if shard_for(key, count) == index
        }
        genesis = {"kind": "genesis", "accounts": opening}
        with self.transaction() as db:
            first = self.row(db, 1)
            if first is None:
                self.append(db, genesis, {})
            elif first["event"] != genesis:
                raise ValueError(
                    "opening configuration changed; refusing to mint new funds"
                )
        self.rebuild()

    # -- event log access ---------------------------------------------------

    def rows(self, db, after=0, until=None):
        """Rows with ``after < seq <= until`` in order; the whole log by default."""
        sql, args = "SELECT body FROM events WHERE seq>?", [after]
        if until is not None:
            sql, args = sql + " AND seq<=?", [after, until]
        return [json.loads(row[0]) for row in db.execute(sql + " ORDER BY seq", args)]

    def row(self, db, seq):
        found = db.execute("SELECT body FROM events WHERE seq=?", (seq,)).fetchone()
        return json.loads(found[0]) if found else None

    def last(self, db):
        found = db.execute(
            "SELECT body FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return json.loads(found[0]) if found else {"seq": 0, "hash": GENESIS_HASH}

    def replay(self, rows):
        """Verify a complete log from genesis and fold it into a new state."""
        verify(rows)
        return apply_events({}, rows)

    def append(self, db, event, state):
        """Append one event and upsert the projection rows it touched.

        ``state`` may be partial (only the rows this event reads); the shared
        reducer then only touches those rows, so a write is O(1), not O(state).
        """
        tail = self.last(db)
        row = entry(tail["seq"] + 1, tail["hash"], event)
        updated = apply_event(state, event)
        db.execute("INSERT INTO events VALUES(?,?)", (row["seq"], encode(row)))
        self.store(db, updated)
        return updated

    # -- row projection -----------------------------------------------------

    def store(self, db, state):
        """Upsert every account/participant row present in ``state``."""
        db.executemany(
            "INSERT OR REPLACE INTO accounts VALUES(?,?,?)",
            [(k, v["available"], v["held"]) for k, v in state["accounts"].items()],
        )
        db.executemany(
            "INSERT OR REPLACE INTO parts VALUES(?,?)",
            [(k, encode(v)) for k, v in state["parts"].items()],
        )

    def replace_projection(self, db, state):
        db.execute("DELETE FROM accounts")
        db.execute("DELETE FROM parts")
        self.store(db, state)

    def account(self, db, key):
        found = db.execute(
            "SELECT available,held FROM accounts WHERE account=?", (key,)
        ).fetchone()
        return {"available": found[0], "held": found[1]} if found else None

    def part(self, db, transaction_id):
        found = db.execute(
            "SELECT body FROM parts WHERE id=?", (transaction_id,)
        ).fetchone()
        return json.loads(found[0]) if found else None

    def projected(self, db):
        """The whole projection, for views and audits (O(state))."""
        return {
            "accounts": {
                row[0]: {"available": row[1], "held": row[2]}
                for row in db.execute("SELECT account,available,held FROM accounts")
            },
            "parts": {
                row[0]: json.loads(row[1])
                for row in db.execute("SELECT id,body FROM parts")
            },
        }

    def touched(self, db, body):
        """Partial state holding only the rows one transfer reads or writes."""
        accounts = {}
        for key in (body["from_account"], body["to_account"]):
            value = self.account(db, key)
            if value is not None:
                accounts[key] = value
        old = self.part(db, body["transaction_id"])
        parts = {body["transaction_id"]: old} if old else {}
        return {"accounts": accounts, "parts": parts}

    # -- snapshots ----------------------------------------------------------

    def load_snapshot(self):
        """Return the checksum-valid snapshot payload, or None when absent."""
        try:
            value = json.loads(self.snapshot_path.read_text())
        except FileNotFoundError:
            return None
        if (
            not isinstance(value, dict)
            or set(value) != {"payload", "checksum"}
            or not isinstance(value["payload"], dict)
            or set(value["payload"]) != {"seq", "head", "state"}
            or not isinstance(value["payload"]["head"], str)
            or not isinstance(value["payload"]["state"], dict)
        ):
            raise ValueError("malformed snapshot")
        payload = value["payload"]
        integer(payload["seq"], 1, 2**62)
        if checksum(payload) != value["checksum"]:
            raise ValueError("snapshot checksum mismatch")
        return payload

    def state_at(self, db, until=None, snapshot=None):
        """Return ``(state, tail_row)`` for log prefix ``until`` (default: all).

        A snapshot at or before ``until`` is the trusted starting point: its
        anchor row must still carry the snapshot head, and only the suffix is
        hash-verified and replayed. Without one, replay starts at genesis.
        """
        if snapshot is None or (until is not None and snapshot["seq"] > until):
            rows = self.rows(db, 0, until)
            return self.replay(rows), rows[-1]
        anchor = self.row(db, snapshot["seq"])
        if (
            anchor is None
            or anchor["hash"] != snapshot["head"]
            or anchor != entry(anchor["seq"], anchor["previous"], anchor["event"])
        ):
            raise ValueError("snapshot does not match the event log")
        rows = self.rows(db, anchor["seq"], until)
        verify(rows, anchor["hash"], anchor["seq"] + 1)
        # load_snapshot parses a fresh object per call, so mutate it directly.
        return apply_events(snapshot["state"], rows), rows[-1] if rows else anchor

    def rebuild(self):
        with self.snapshot_lock:
            return self._rebuild()

    def _rebuild(self):
        with self.transaction() as db:
            snapshot = self.load_snapshot()
            if snapshot is not None and snapshot["seq"] > self.last(db)["seq"]:
                raise ValueError("snapshot is ahead of the event log")
            state, _ = self.state_at(db, snapshot=snapshot)
            self.replace_projection(db, state)
            return state

    def snapshot(self):
        with self.snapshot_lock:
            return self._snapshot()

    def _snapshot(self):
        with self.read_transaction() as db:
            # Incremental: previous snapshot plus the verified suffix.
            state, tail = self.state_at(db, snapshot=self.load_snapshot())
            payload = {"seq": tail["seq"], "head": tail["hash"], "state": state}
        path = self.snapshot_path
        # Own publication separately from the money writer. Unique files avoid
        # collisions; serialization also keeps snapshot prefixes monotonic.
        descriptor, temporary = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(
                    encode({"payload": payload, "checksum": checksum(payload)})
                )
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(10):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    # Windows may briefly retain a just-closed destination.
                    time.sleep(0.01 * (attempt + 1))
        finally:
            Path(temporary).unlink(missing_ok=True)
        return {"seq": payload["seq"], "head": payload["head"]}

    # -- participant commands -----------------------------------------------

    def action(self, body, kind, role, epoch, leader):
        body = intent(body)
        if (
            role not in ("source", "destination", "local")
            or kind not in ("local", "prepare", "confirm", "cancel")
            or (kind == "local") != (role == "local")
        ):
            raise ValueError("invalid participant action")
        account = body["from_account"] if role != "destination" else body["to_account"]
        if shard_for(account, self.count) != self.index or (
            role == "local" and shard_for(body["to_account"], self.count) != self.index
        ):
            raise Error(409, "wrong shard")
        with self.transaction() as db:
            self.check_fence(db, epoch, leader)
            state = self.touched(db, body)
            old = state["parts"].get(body["transaction_id"])
            if old:
                if old["intent"] != body or old["role"] != role:
                    raise Error(409, "participant command conflict")
                if (
                    kind in ("prepare", "local")
                    or (kind == "confirm" and old["status"] == "CONFIRMED")
                    or (kind == "cancel" and old["status"] == "CANCELED")
                ):
                    return old
                if (
                    kind == "confirm"
                    and old["status"] != "PREPARED"
                    or kind == "cancel"
                    and old["status"] == "CONFIRMED"
                ):
                    raise Error(409, "participant terminal conflict")
            elif kind == "confirm":
                raise Error(409, "confirm without prepare")
            state = self.append(db, {"kind": kind, "role": role, "intent": body}, state)
            return state["parts"][body["transaction_id"]]

    # -- queries ------------------------------------------------------------

    def view(self, at=None):
        with self.read_transaction() as db:
            if at is None:
                tail = self.last(db)
                state = self.projected(db)
            else:
                at = integer(at, 1, self.last(db)["seq"])
                state, tail = self.historical(db, at)
            return {
                "shard": self.index,
                "seq": tail["seq"],
                "head": tail["hash"],
                **state,
            }

    def historical(self, db, at):
        """State as of ``at``: snapshot + tail when possible, else from genesis.

        Reads stay available if the snapshot is unusable; the full replay is
        still hash-verified, while rebuild and audit report the bad snapshot.
        """
        try:
            snapshot = self.load_snapshot()
            return self.state_at(db, at, snapshot)
        except ValueError:
            return self.state_at(db, at)

    def participant(self, transaction_id):
        identifier(transaction_id)
        with self.read_transaction() as db:
            return self.part(db, transaction_id)

    def balance(self, account, at=None):
        identifier(account)
        if at is None:
            # Current balance: one row and the log tail from one read snapshot.
            with self.read_transaction() as db:
                value, seq = self.account(db, account), self.last(db)["seq"]
            if value is None:
                raise Error(404, "account missing")
            return {
                "account": account,
                "currency": "USD",
                "shard": self.index,
                "sequence": seq,
                **value,
            }
        view = self.view(at)
        if account not in view["accounts"]:
            raise Error(404, "account missing")
        return {
            "account": account,
            "currency": "USD",
            "shard": view["shard"],
            "sequence": view["seq"],
            **view["accounts"][account],
        }

    def audit(self):
        """Full-history check of the log, the projection and the snapshot.

        Holding the publication lock keeps the snapshot file within this read
        transaction's log prefix; money writers are not blocked.
        """
        with self.snapshot_lock, self.read_transaction() as db:
            rows = self.rows(db)
            verify(rows)
            try:
                snapshot = self.load_snapshot()
            except ValueError:
                snapshot, snapshot_ok = None, False
            else:
                snapshot_ok = snapshot is None or snapshot["seq"] <= len(rows)
            state = {}
            for row in rows:
                state = apply_event(state, row["event"])
                if snapshot is not None and row["seq"] == snapshot["seq"]:
                    snapshot_ok = (
                        row["hash"] == snapshot["head"] and state == snapshot["state"]
                    )
            projected = self.projected(db)
            return {
                "matches": state == projected and snapshot_ok,
                "events": len(rows),
                "head": rows[-1]["hash"],
            }
