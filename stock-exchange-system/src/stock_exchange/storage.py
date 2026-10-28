import json
import threading
from pathlib import Path

from .common import Database, Error, checksum, encode, entry, integer, verify
from .matching import audit, book, reduce_command
from .contracts import (
    freeze,
    fields,
    settings as validate_settings,
    validate,
    MAX_TIMESTAMP,
)
from .snapshots import compatible, publish


class Log(Database):
    def __init__(self, path):
        super().__init__(path)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,body TEXT);
        CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable journal'); END;
        CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable journal'); END;
        """)
        with self.connection() as db:
            verify(self.rows(db))

    def rows(self, db):
        return [
            json.loads(row[0])
            for row in db.execute("SELECT body FROM events ORDER BY seq")
        ]

    def tail(self, db):
        row = db.execute("SELECT body FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else {"seq": 0, "hash": "0" * 64}

    def events(self, after=0, limit=100):
        integer(after)
        integer(limit, 1, 100)
        with self.read_transaction() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT body FROM events WHERE seq>? ORDER BY seq LIMIT ?",
                    (after, limit),
                )
            ]

    def cursor(self):
        with self.read_transaction() as db:
            tail = self.tail(db)
            return {"seq": tail["seq"], "head": tail["hash"]}


class Sequencer(Log):
    def __init__(self, path, settings, timestamp):
        settings = validate_settings(settings)
        self.settings = settings
        super().__init__(path)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS requests(owner TEXT,id TEXT,hash TEXT,seq INTEGER,PRIMARY KEY(owner,id));
        CREATE TABLE IF NOT EXISTS control(id INTEGER PRIMARY KEY CHECK(id=1),epoch INTEGER,active TEXT,timestamp INTEGER);
        """)
        with self.transaction() as db:
            rows = self.rows(db)
            genesis = {"kind": "genesis", "settings": settings}
            if not rows:
                db.execute(
                    "INSERT INTO events VALUES(1,?)",
                    (encode(entry(1, "0" * 64, genesis)),),
                )
            elif rows[0]["event"] != genesis:
                raise ValueError(
                    "opening accounts/rules changed; refusing non-deterministic replay"
                )
            db.execute(
                "INSERT OR IGNORE INTO control VALUES(1,1,'primary',?)",
                (integer(timestamp, 1, MAX_TIMESTAMP),),
            )
            # The deduplication index is part of durable acceptance, not an
            # optional cache. Refuse a damaged/missing index on retained history.
            replay, expected = {}, set()
            for row in self.rows(db):
                event = row["event"]
                replay = reduce_command(replay, event)
                if event["kind"] != "genesis":
                    expected.add(
                        (
                            event["owner"],
                            event["body"]["request_id"],
                            checksum([event["kind"], event["body"]]),
                            row["seq"],
                        )
                    )
            actual = {
                tuple(row)
                for row in db.execute("SELECT owner,id,hash,seq FROM requests")
            }
            if actual != expected:
                raise ValueError(
                    "durable request deduplication index differs from history"
                )
            control = db.execute("SELECT * FROM control").fetchone()
            if control["timestamp"] < replay["last_timestamp"] or control[
                "active"
            ] not in {"primary", "backup"}:
                raise ValueError("sequencer control regressed")
            integer(control["epoch"], 1)

    def submit(self, owner, kind, body):
        body = validate(body, kind)
        if owner not in self.settings["accounts"]:
            raise Error(403, "known synthetic owner required")
        fingerprint = checksum([kind, body])
        with self.transaction() as db:
            old = db.execute(
                "SELECT * FROM requests WHERE owner=? AND id=?",
                (owner, body["request_id"]),
            ).fetchone()
            if old:
                if old["hash"] != fingerprint:
                    raise Error(409, "request identity content conflict")
                return {
                    "seq": old["seq"],
                    "request_id": body["request_id"],
                    "status": "ACCEPTED",
                    "duplicate": True,
                }
            tail = self.tail(db)
            if tail["seq"] >= 1001:
                raise Error(429, "reference limit of 1000 commands reached")
            seq = tail["seq"] + 1
            stamp = db.execute("SELECT timestamp FROM control").fetchone()[0]
            command = {
                "seq": seq,
                "timestamp": stamp,
                "owner": owner,
                "kind": kind,
                "body": body,
            }
            row = entry(seq, tail["hash"], command)
            db.execute("INSERT INTO events VALUES(?,?)", (seq, encode(row)))
            db.execute(
                "INSERT INTO requests VALUES(?,?,?,?)",
                (owner, body["request_id"], fingerprint, seq),
            )
        return {
            "seq": seq,
            "request_id": body["request_id"],
            "status": "ACCEPTED",
            "duplicate": False,
        }

    def lookup(self, owner, id):
        with self.connection() as db:
            row = db.execute(
                "SELECT seq FROM requests WHERE owner=? AND id=?", (owner, id)
            ).fetchone()
            if not row:
                raise Error(404, "owned request missing")
            return row[0]

    def control(self):
        with self.read_transaction() as db:
            row = dict(db.execute("SELECT * FROM control").fetchone())
            tail = self.tail(db)
            return {
                "epoch": row["epoch"],
                "active": row["active"],
                "timestamp": row["timestamp"],
                "seq": tail["seq"],
                "head": tail["hash"],
            }

    def clock(self, timestamp):
        integer(timestamp, 1, MAX_TIMESTAMP)
        with self.transaction() as db:
            if timestamp < db.execute("SELECT timestamp FROM control").fetchone()[0]:
                raise Error(409, "simulation clock cannot move backwards")
            db.execute("UPDATE control SET timestamp=?", (timestamp,))
        return self.control()

    def promote(self, node, replica):
        replica = freeze(replica)
        if node not in ("primary", "backup"):
            raise ValueError("unknown matching replica")
        fields(replica, {"seq", "head", "state", "state_hash"})
        integer(replica["seq"], 0, 1001)
        if checksum(replica["state"]) != replica["state_hash"]:
            raise Error(409, "candidate state checksum mismatch")
        with self.transaction() as db:
            rows = self.rows(db)
            if replica["seq"] != len(rows) or replica["head"] != rows[-1]["hash"]:
                raise Error(409, "promotion requires complete sequencer prefix")
            expected = {}
            for row in rows:
                expected = reduce_command(expected, row["event"])
            if replica["state"] != expected:
                raise Error(
                    409, "candidate state differs from independent sequencer replay"
                )
            current = db.execute("SELECT active FROM control").fetchone()[0]
            if node != current:
                db.execute("UPDATE control SET epoch=epoch+1,active=?", (node,))
        return self.control()


def market_batch(previous, state, row):
    symbols = state["settings"]["symbols"]
    updates = {}
    for symbol in symbols:
        before = book(previous, symbol) if previous else {"bids": {}, "asks": {}}
        after = book(state, symbol)
        sides = {
            side: {
                price: after[side].get(price, 0)
                for price in set(before[side]) | set(after[side])
                if before[side].get(price, 0) != after[side].get(price, 0)
            }
            for side in ("bids", "asks")
        }
        if any(sides.values()):
            updates[symbol] = sides
    return {
        "seq": row["seq"],
        "previous": row["previous"],
        "head": row["hash"],
        "state_hash": checksum(state),
        "books": updates,
        "orders": [
            value
            for key, value in state["orders"].items()
            if not previous or previous["orders"].get(key) != value
        ],
        "trades": state["trades"][len(previous["trades"]) if previous else 0 :],
    }


class Engine(Log):
    def __init__(self, path, retention=16):
        super().__init__(path)
        self.retention = integer(retention, 2, 1000)
        self.snapshot_lock = threading.RLock()
        self.initialize("""
        CREATE TABLE IF NOT EXISTS projection(id INTEGER PRIMARY KEY CHECK(id=1),state TEXT);
        CREATE TABLE IF NOT EXISTS batches(seq INTEGER PRIMARY KEY,body TEXT);
        CREATE TABLE IF NOT EXISTS fence(id INTEGER PRIMARY KEY CHECK(id=1),epoch INTEGER);
        INSERT OR IGNORE INTO fence VALUES(1,0);
        """)
        self.rebuild()

    def replay(self, rows):
        verify(rows)
        state = {}
        for row in rows:
            state = reduce_command(state, row["event"])
        return state

    def rebuild(self):
        with self.snapshot_lock:
            return self._rebuild()

    def _rebuild(self):
        with self.transaction() as db:
            rows = self.rows(db)
            state = self.replay(rows)
            path = Path(str(self.path) + ".snapshot.json")
            if path.exists():
                value = json.loads(path.read_text())
                payload = value["payload"]
                seq = integer(payload["seq"], 1, len(rows))
                prefix = self.replay(rows[:seq])
                if (
                    value["checksum"] != checksum(payload)
                    or payload["head"] != rows[seq - 1]["hash"]
                    or not compatible(payload["state"], prefix)
                ):
                    raise ValueError("snapshot checksum or replay mismatch")
                state = prefix
                for row in rows[seq:]:
                    state = reduce_command(state, row["event"])
            db.execute("DELETE FROM batches")
            previous = {}
            for row in rows:
                updated = reduce_command(previous, row["event"])
                if row["seq"] > len(rows) - self.retention:
                    db.execute(
                        "INSERT INTO batches VALUES(?,?)",
                        (row["seq"], encode(market_batch(previous, updated, row))),
                    )
                previous = updated
            db.execute(
                "INSERT OR REPLACE INTO projection VALUES(1,?)", (encode(state),)
            )

    def apply(self, row):
        row = freeze(row)
        fields(row, {"seq", "previous", "event", "hash"})
        integer(row["seq"], 1, 1001)
        if row["event"]["kind"] != "genesis" and row["event"]["seq"] != row["seq"]:
            raise ValueError("command and journal sequence disagree")
        with self.transaction() as db:
            tail = self.tail(db)
            seq = row["seq"]
            if seq <= tail["seq"]:
                old = db.execute(
                    "SELECT body FROM events WHERE seq=?", (seq,)
                ).fetchone()
                if old is None or json.loads(old[0]) != row:
                    raise Error(409, "matching replica prefix conflict")
                return
            if row != entry(tail["seq"] + 1, tail["hash"], row["event"]):
                raise Error(409, "command sequence/checksum gap")
            state = json.loads(db.execute("SELECT state FROM projection").fetchone()[0])
            if state and not audit(state)["matches"]:
                raise Error(
                    409, "read projection failed audit; rebuild before matching"
                )
            updated = reduce_command(state, row["event"])
            batch = market_batch(state, updated, row)
            db.execute("INSERT INTO events VALUES(?,?)", (seq, encode(row)))
            db.execute(
                "INSERT OR REPLACE INTO projection VALUES(1,?)", (encode(updated),)
            )
            db.execute("INSERT INTO batches VALUES(?,?)", (seq, encode(batch)))
            db.execute("DELETE FROM batches WHERE seq<=?", (seq - self.retention,))

    def raw(self):
        with self.read_transaction() as db:
            tail = self.tail(db)
            state = json.loads(db.execute("SELECT state FROM projection").fetchone()[0])
            return {
                "seq": tail["seq"],
                "head": tail["hash"],
                "state": state,
                "state_hash": checksum(state),
            }

    def query(self, kind, key, owner):
        with self.read_transaction() as db:
            tail = self.tail(db)
            state = json.loads(db.execute("SELECT state FROM projection").fetchone()[0])
            if not tail["seq"] or owner not in state["accounts"]:
                raise Error(503, "engine/account not ready")
            if kind == "account":
                value = state["accounts"][owner]
            elif kind == "order":
                value = state["orders"].get(key)
                if value is None or value["owner"] != owner:
                    raise Error(404, "owned order missing")
            elif kind == "request":
                sequence = integer(int(key), 2, 1001)
                value = state["results"].get(
                    str(sequence), {"seq": sequence, "status": "PENDING"}
                )
                if sequence <= tail["seq"]:
                    row = db.execute(
                        "SELECT body FROM events WHERE seq=?", (sequence,)
                    ).fetchone()
                    if row is None or json.loads(row[0])["event"].get("owner") != owner:
                        raise Error(404, "owned request missing")
            else:
                raise ValueError("unsupported engine query")
            return {
                "seq": tail["seq"],
                "head": tail["hash"],
                "kind": kind,
                "key": key,
                "owner": owner,
                "value": value,
            }

    def fence(self, epoch):
        integer(epoch, 1)
        with self.transaction() as db:
            previous = db.execute("SELECT epoch FROM fence").fetchone()[0]
            if epoch < previous:
                raise Error(409, "sequencer epoch regressed; recovery refused")
            if epoch != previous:
                db.execute("UPDATE fence SET epoch=?", (epoch,))

    def feed(self, after):
        integer(after)
        with self.read_transaction() as db:
            if after > self.tail(db)["seq"]:
                raise Error(409, "feed cursor ahead of engine")
            oldest = db.execute("SELECT min(seq) FROM batches").fetchone()[0]
            if oldest is not None and after < oldest - 1:
                raise Error(410, "market delta gap; fetch a new snapshot")
            return {
                "batches": [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT body FROM batches WHERE seq>? ORDER BY seq", (after,)
                    )
                ]
            }

    def snapshot(self):
        with self.snapshot_lock:
            view = self.raw()
            if not view["seq"]:
                raise Error(503, "engine has not loaded genesis")
            payload = {key: view[key] for key in ("seq", "head", "state")}
            publish(Path(str(self.path) + ".snapshot.json"), payload)
            return {"seq": view["seq"], "state_hash": view["state_hash"]}

    def audit(self):
        with self.read_transaction() as db:
            rows = self.rows(db)
            replay = self.replay(rows)
            projected = json.loads(
                db.execute("SELECT state FROM projection").fetchone()[0]
            )
            if not replay:
                raise Error(503, "engine not ready")
            return {
                "seq": len(rows),
                "projection_matches": replay == projected,
                **audit(replay),
            }
