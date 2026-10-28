import json
import os
import tempfile
import threading
import time
from pathlib import Path

from .common import (
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
from .projection import reduce_event as reduce_event


class Shard(Fence):
    def __init__(self, path, index, count, accounts, epoch=1, leader="coordinator"):
        super().__init__(path)
        integer(count, 1, 100)
        integer(index, 0, count - 1)
        self.index, self.count = index, count
        self.snapshot_lock = threading.RLock()
        self.init_fence(epoch, leader)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,body TEXT);
        CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable event'); END;
        CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable event'); END;
        CREATE TABLE IF NOT EXISTS projection(id INTEGER PRIMARY KEY CHECK(id=1),state TEXT);
        """)
        opening = {
            key: integer(value)
            for key, value in accounts.items()
            if shard_for(key, count) == index
        }
        with self.transaction() as db:
            rows = self.rows(db)
            if not rows:
                self.append(db, {"kind": "genesis", "accounts": opening}, {})
            elif rows[0]["event"] != {"kind": "genesis", "accounts": opening}:
                raise ValueError(
                    "opening configuration changed; refusing to mint new funds"
                )
        self.rebuild()

    def rows(self, db):
        return [
            json.loads(row[0])
            for row in db.execute("SELECT body FROM events ORDER BY seq")
        ]

    def replay(self, rows):
        verify(rows)
        state = {}
        for row in rows:
            state = reduce_event(state, row["event"])
        return state

    def append(self, db, event, state):
        previous = db.execute(
            "SELECT body FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        tail = json.loads(previous[0]) if previous else {"seq": 0, "hash": "0" * 64}
        row = entry(tail["seq"] + 1, tail["hash"], event)
        updated = reduce_event(state, event)
        db.execute("INSERT INTO events VALUES(?,?)", (row["seq"], encode(row)))
        db.execute("INSERT OR REPLACE INTO projection VALUES(1,?)", (encode(updated),))
        return updated

    def rebuild(self):
        with self.snapshot_lock:
            return self._rebuild()

    def _rebuild(self):
        with self.transaction() as db:
            rows = self.rows(db)
            state = self.replay(rows)
            snapshot = Path(str(self.path) + ".snapshot.json")
            if snapshot.exists():
                value = json.loads(snapshot.read_text())
                payload = value["payload"]
                seq = integer(payload["seq"], 1, len(rows))
                if (
                    checksum(payload) != value["checksum"]
                    or payload["head"] != rows[seq - 1]["hash"]
                    or payload["state"] != self.replay(rows[:seq])
                ):
                    raise ValueError("snapshot checksum or projection mismatch")
                state = payload["state"]
                for row in rows[seq:]:
                    state = reduce_event(state, row["event"])
            db.execute(
                "INSERT OR REPLACE INTO projection VALUES(1,?)", (encode(state),)
            )
            return state

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
            state = json.loads(db.execute("SELECT state FROM projection").fetchone()[0])
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

    def view(self, at=None):
        with self.read_transaction() as db:
            if at is None:
                tail = json.loads(
                    db.execute(
                        "SELECT body FROM events ORDER BY seq DESC LIMIT 1"
                    ).fetchone()[0]
                )
                state = json.loads(
                    db.execute("SELECT state FROM projection").fetchone()[0]
                )
            else:
                rows = self.rows(db)
                at = integer(at, 1, len(rows))
                tail = rows[at - 1]
                state = self.replay(rows[:at])
            return {
                "shard": self.index,
                "seq": tail["seq"],
                "head": tail["hash"],
                **state,
            }

    def participant(self, transaction_id):
        identifier(transaction_id)
        return self.view()["parts"].get(transaction_id)

    def balance(self, account, at=None):
        identifier(account)
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

    def snapshot(self):
        with self.snapshot_lock:
            return self._snapshot()

    def _snapshot(self):
        with self.read_transaction() as db:
            rows = self.rows(db)
            state = self.replay(rows)
            payload = {"seq": len(rows), "head": rows[-1]["hash"], "state": state}
        path = Path(str(self.path) + ".snapshot.json")
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

    def audit(self):
        with self.read_transaction() as db:
            replayed = self.replay(self.rows(db))
            projected = json.loads(
                db.execute("SELECT state FROM projection").fetchone()[0]
            )
            return {
                "matches": replayed == projected,
                "events": db.execute("SELECT count(*) FROM events").fetchone()[0],
                "head": self.rows(db)[-1]["hash"],
            }
