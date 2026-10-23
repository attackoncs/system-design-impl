"""An immutable partitioned HTTP queue, also the raw-data replay source.

This SQLite queue has one durable owner, not replicated broker HA.
"""

import time
import uuid

from .common import Database, Error, digest, dumps, event, integer


class EventLog(Database):
    def __init__(self, path, partitions=4, max_events=1_000_000, clock=time.time):
        super().__init__(path)
        integer(partitions, "partitions", 1, 64)
        self.partitions, self.max_events, self.clock = partitions, max_events, clock
        self.initialize("""
        CREATE TABLE IF NOT EXISTS metadata(id INTEGER PRIMARY KEY CHECK(id=1), lineage TEXT, partitions INTEGER);
        CREATE TABLE IF NOT EXISTS heads(partition INTEGER PRIMARY KEY, seq INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, hash TEXT NOT NULL,
            partition INTEGER NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL, accepted REAL NOT NULL,
            UNIQUE(partition,seq));
        """)
        with self.transaction(True) as db:
            db.execute(
                "INSERT OR IGNORE INTO metadata VALUES(1,?,?)",
                (uuid.uuid4().hex, partitions),
            )
            meta = db.execute("SELECT * FROM metadata").fetchone()
            if meta["partitions"] != partitions:
                raise Error(
                    409, "partition count is immutable; offline migration required"
                )
            self.lineage = meta["lineage"]
            db.executemany(
                "INSERT OR IGNORE INTO heads VALUES(?,0)",
                [(p,) for p in range(partitions)],
            )

    def append(self, bodies):
        if not isinstance(bodies, list) or not 1 <= len(bodies) <= 200:
            raise ValueError("events must contain 1..200 items")
        now = self.clock()
        normalized = [event(body, now) for body in bodies]
        results = []
        with self.transaction(True) as db:
            used = db.execute("SELECT count(*) FROM events").fetchone()[0]
            for value in normalized:
                fingerprint = digest(value)
                existing = db.execute(
                    "SELECT * FROM events WHERE event_id=?", (value["event_id"],)
                ).fetchone()
                if existing:
                    if existing["hash"] != fingerprint:
                        raise Error(409, "event ID reused with different payload")
                    results.append(
                        {
                            "partition": existing["partition"],
                            "seq": existing["seq"],
                            "duplicate": True,
                        }
                    )
                    continue
                if used >= self.max_events:
                    raise Error(
                        429,
                        "raw log capacity reached; archive/migrate before continuing",
                    )
                # Salt by event identity so even a single hot ad spans workers.
                partition = int(digest(value["event_id"])[:16], 16) % self.partitions
                seq = (
                    db.execute(
                        "SELECT seq FROM heads WHERE partition=?", (partition,)
                    ).fetchone()[0]
                    + 1
                )
                db.execute("UPDATE heads SET seq=? WHERE partition=?", (seq, partition))
                db.execute(
                    "INSERT INTO events VALUES(?,?,?,?,?,?)",
                    (value["event_id"], fingerprint, partition, seq, dumps(value), now),
                )
                used += 1
                results.append({"partition": partition, "seq": seq, "duplicate": False})
        return {"lineage": self.lineage, "receipts": results}

    def read(self, partition, after, limit=200, until=None):
        integer(partition, "partition", 0, self.partitions - 1)
        integer(after, "after", 0, 2**53)
        integer(limit, "limit", 1, 200)
        with self.transaction() as db:
            head = db.execute(
                "SELECT seq FROM heads WHERE partition=?", (partition,)
            ).fetchone()[0]
            if after > head:
                raise Error(409, "cursor ahead of raw log")
            upper = (
                head if until is None else min(integer(until, "until", 0, 2**53), head)
            )
            rows = db.execute(
                "SELECT seq,payload,accepted,hash FROM events WHERE partition=? AND seq>? AND seq<=? ORDER BY seq LIMIT ?",
                (partition, after, upper, limit),
            ).fetchall()
        import json

        return {
            "lineage": self.lineage,
            "partition": partition,
            "head": head,
            "entries": [
                {
                    "seq": r["seq"],
                    "event": json.loads(r["payload"]),
                    "accepted": r["accepted"],
                    "hash": r["hash"],
                }
                for r in rows
            ],
        }

    def status(self):
        with self.transaction() as db:
            return {
                "lineage": self.lineage,
                "heads": {str(r[0]): r[1] for r in db.execute("SELECT * FROM heads")},
                "raw_events": db.execute("SELECT count(*) FROM events").fetchone()[0],
                "capacity": self.max_events,
            }
