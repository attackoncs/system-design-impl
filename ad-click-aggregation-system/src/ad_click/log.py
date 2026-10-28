"""Immutable partitioned raw events with short, atomic batched writes.

One process owns the durable source; this is not replicated broker HA.
"""

import time
import uuid

from .common import Database, Error, digest, dumps, integer
from .records import MAX_POSITION, parse_source_row, prepare_events, timestamp


class EventLog(Database):
    def __init__(self, path, partitions=4, max_events=1_000_000, clock=time.time):
        integer(partitions, "partitions", 1, 64)
        integer(max_events, "max_events", 1, MAX_POSITION)
        if not callable(clock):
            raise ValueError("clock must be callable")
        super().__init__(path)
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
        normalized = prepare_events(bodies)
        # CPU work and copying precede SQLite's single writer. Only acceptance
        # time, capacity, duplicate identity and sequence allocation need it.
        prepared = [
            (
                value,
                digest(value),
                dumps(value),
                int(digest(value["event_id"])[:16], 16) % self.partitions,
            )
            for value in normalized
        ]
        results = []
        with self.transaction(True) as db:
            now = timestamp(self.clock(), "clock")
            heads = dict(db.execute("SELECT partition,seq FROM heads"))
            used = sum(heads.values())
            ids = tuple({value["event_id"] for value in normalized})
            existing = {
                row["event_id"]: dict(row)
                for row in db.execute(
                    "SELECT event_id,hash,partition,seq FROM events WHERE event_id IN ("
                    + ",".join("?" for _ in ids)
                    + ")",
                    ids,
                )
            }
            rows = []
            changed = set()
            for value, fingerprint, payload, partition in prepared:
                prior = existing.get(value["event_id"])
                if prior:
                    if prior["hash"] != fingerprint:
                        raise Error(409, "event ID reused with different payload")
                    results.append(
                        {
                            "partition": prior["partition"],
                            "seq": prior["seq"],
                            "duplicate": True,
                        }
                    )
                    continue
                if value["click_timestamp"] > int(now) + 30:
                    raise ValueError("event exceeds future tolerance")
                if used >= self.max_events:
                    raise Error(
                        429,
                        "raw log capacity reached; archive/migrate before continuing",
                    )
                seq = heads[partition] + 1
                integer(seq, "sequence", 1, MAX_POSITION)
                heads[partition] = seq
                changed.add(partition)
                existing[value["event_id"]] = {
                    "hash": fingerprint,
                    "partition": partition,
                    "seq": seq,
                }
                rows.append(
                    (value["event_id"], fingerprint, partition, seq, payload, now)
                )
                used += 1
                results.append({"partition": partition, "seq": seq, "duplicate": False})
            db.executemany("INSERT INTO events VALUES(?,?,?,?,?,?)", rows)
            db.executemany(
                "UPDATE heads SET seq=? WHERE partition=?",
                [(heads[p], p) for p in changed],
            )
        return {"lineage": self.lineage, "receipts": results}

    def read(self, partition, after, limit=200, until=None):
        integer(partition, "partition", 0, self.partitions - 1)
        integer(after, "after", 0, MAX_POSITION)
        integer(limit, "limit", 1, 200)
        with self.transaction() as db:
            head = db.execute(
                "SELECT seq FROM heads WHERE partition=?", (partition,)
            ).fetchone()[0]
            if after > head:
                raise Error(409, "cursor ahead of raw log")
            upper = (
                head
                if until is None
                else min(integer(until, "until", 0, MAX_POSITION), head)
            )
            rows = db.execute(
                "SELECT seq,payload,accepted,hash FROM events WHERE partition=? AND seq>? AND seq<=? ORDER BY seq LIMIT ?",
                (partition, after, upper, limit),
            ).fetchall()
        entries = [parse_source_row(row) for row in rows]
        expected = min(limit, max(0, upper - after))
        if len(entries) != expected or any(
            item["seq"] != after + i + 1 for i, item in enumerate(entries)
        ):
            raise Error(503, "persisted raw log offset gap")
        return {
            "lineage": self.lineage,
            "partition": partition,
            "head": head,
            "entries": entries,
        }

    def status(self):
        with self.transaction() as db:
            heads = {str(row[0]): row[1] for row in db.execute("SELECT * FROM heads")}
            return {
                "lineage": self.lineage,
                "heads": heads,
                "raw_events": sum(heads.values()),
                "capacity": self.max_events,
            }
