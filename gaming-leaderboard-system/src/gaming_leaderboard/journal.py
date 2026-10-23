import json
import time
import uuid

from .common import (
    Database,
    Error,
    current_month,
    digest,
    dumps,
    identifier,
    integer,
    month_bounds,
    shard_for,
)


class Journal(Database):
    def __init__(self, path, shards=2, grace=172800, clock=time.time):
        super().__init__(path)
        self.shards, self.grace, self.clock = shards, grace, clock
        integer(shards, "shards", 1, 16)
        integer(grace, "grace", 0, 604800)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS metadata(id INTEGER PRIMARY KEY CHECK(id=1),lineage TEXT,shards INTEGER,grace INTEGER);
        CREATE TABLE IF NOT EXISTS players(id TEXT PRIMARY KEY,name TEXT,registered INTEGER);
        CREATE TABLE IF NOT EXISTS seasons(month TEXT PRIMARY KEY,status TEXT,settlement TEXT);
        CREATE TABLE IF NOT EXISTS scores(month TEXT,user TEXT,score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 1000000000),PRIMARY KEY(month,user));
        CREATE TABLE IF NOT EXISTS matches(id TEXT PRIMARY KEY,hash TEXT,month TEXT,receipt TEXT);
        CREATE TABLE IF NOT EXISTS heads(shard INTEGER PRIMARY KEY,seq INTEGER);
        CREATE TABLE IF NOT EXISTS events(shard INTEGER,seq INTEGER,payload TEXT,hash TEXT,PRIMARY KEY(shard,seq));
        """)
        with self.transaction(True) as db:
            db.execute(
                "INSERT OR IGNORE INTO metadata VALUES(1,?,?,?)",
                (uuid.uuid4().hex, shards, grace),
            )
            meta = db.execute("SELECT * FROM metadata").fetchone()
            if (meta["shards"], meta["grace"]) != (shards, grace):
                raise Error(
                    409, "journal topology/grace differs; explicit migration required"
                )
            self.lineage = meta["lineage"]
            db.executemany(
                "INSERT OR IGNORE INTO heads VALUES(?,0)", [(p,) for p in range(shards)]
            )

    def emit(self, db, month, user, score):
        shard = shard_for(user, self.shards)
        seq = (
            db.execute("SELECT seq FROM heads WHERE shard=?", (shard,)).fetchone()[0]
            + 1
        )
        payload = {"month": month, "user": user, "score": score}
        db.execute(
            "INSERT INTO events VALUES(?,?,?,?)",
            (shard, seq, dumps(payload), digest(payload)),
        )
        db.execute("UPDATE heads SET seq=? WHERE shard=?", (seq, shard))

    def ensure(self, db, month):
        _, end = month_bounds(month)
        db.execute("INSERT OR IGNORE INTO seasons VALUES(?,'open',NULL)", (month,))
        for player in db.execute(
            "SELECT id FROM players WHERE registered<? ORDER BY id", (end,)
        ).fetchall():
            if db.execute(
                "INSERT OR IGNORE INTO scores VALUES(?,?,0)", (month, player[0])
            ).rowcount:
                self.emit(db, month, player[0], 0)

    def register(self, user, name):
        identifier(user)
        if not isinstance(name, str) or not 1 <= len(name) <= 128:
            raise ValueError("display name requires 1..128 characters")
        with self.transaction(True) as db:
            if (
                not db.execute("SELECT 1 FROM players WHERE id=?", (user,)).fetchone()
                and db.execute("SELECT count(*) FROM players").fetchone()[0] >= 10000
            ):
                raise Error(429, "reference player capacity reached")
            db.execute(
                "INSERT INTO players VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                (user, name, int(self.clock())),
            )
            self.ensure(db, current_month(self.clock()))
        return {"user": user, "name": name}

    def month(self, month=None):
        month = month or current_month(self.clock())
        month_bounds(month)
        with self.transaction(True) as db:
            if month == current_month(self.clock()):
                self.ensure(db, month)
            row = db.execute("SELECT * FROM seasons WHERE month=?", (month,)).fetchone()
            if not row:
                raise Error(404, "season missing")
            result = dict(row)
            result["settlement"] = (
                json.loads(result["settlement"]) if result["settlement"] else None
            )
            result["accept_until"] = month_bounds(month)[1] + self.grace
            return result

    def ingest(self, body, before_commit=None):
        if not isinstance(body, dict) or set(body) != {
            "match_id",
            "winner",
            "players",
            "ended_at",
        }:
            raise ValueError(
                "match requires match_id,winner,players,ended_at; points are fixed at one"
            )
        match, winner = identifier(body["match_id"]), identifier(body["winner"])
        players = body["players"]
        if (
            not isinstance(players, list)
            or not 2 <= len(players) <= 8
            or len({identifier(p) for p in players}) != len(players)
            or winner not in players
        ):
            raise ValueError(
                "2..8 distinct registered participants including winner required"
            )
        integer(body["ended_at"], "ended_at")
        normalized = {**body, "players": sorted(players)}
        fingerprint = digest(normalized)
        with self.transaction(True) as db:
            old = db.execute("SELECT * FROM matches WHERE id=?", (match,)).fetchone()
            if old:
                if old["hash"] != fingerprint:
                    raise Error(409, "match ID reused with different result")
                return {**json.loads(old["receipt"]), "duplicate": True}
            if body["ended_at"] > self.clock() + 30:
                raise ValueError("match is too far in the future")
            month = current_month(body["ended_at"])
            if self.clock() >= month_bounds(month)[1] + self.grace:
                raise Error(409, "late acceptance deadline has passed")
            if month > current_month(self.clock()):
                raise ValueError("future-month match is not accepted")
            for user in players:
                row = db.execute(
                    "SELECT registered FROM players WHERE id=?", (user,)
                ).fetchone()
                if not row or row[0] > body["ended_at"]:
                    raise Error(
                        409, "participant was not registered when the match ended"
                    )
            self.ensure(db, month)
            if (
                db.execute(
                    "SELECT status FROM seasons WHERE month=?", (month,)
                ).fetchone()[0]
                != "open"
            ):
                raise Error(409, "season already settled")
            if not db.execute(
                "UPDATE scores SET score=score+1 WHERE month=? AND user=? AND score<1000000000",
                (month, winner),
            ).rowcount:
                raise Error(429, "reference score limit reached")
            score = db.execute(
                "SELECT score FROM scores WHERE month=? AND user=?", (month, winner)
            ).fetchone()[0]
            self.emit(db, month, winner, score)
            receipt = {
                "match_id": match,
                "month": month,
                "winner": winner,
                "score": score,
                "heads": {str(r[0]): r[1] for r in db.execute("SELECT * FROM heads")},
                "duplicate": False,
            }
            db.execute(
                "INSERT INTO matches VALUES(?,?,?,?)",
                (match, fingerprint, month, dumps(receipt)),
            )
            if before_commit:
                before_commit()
            return receipt

    def status(self):
        with self.transaction() as db:
            return {
                "lineage": self.lineage,
                "shards": self.shards,
                "heads": {str(r[0]): r[1] for r in db.execute("SELECT * FROM heads")},
                "matches": db.execute("SELECT count(*) FROM matches").fetchone()[0],
            }

    def feed(self, shard, after):
        integer(shard, "shard", 0, self.shards - 1)
        integer(after, "after")
        with self.transaction() as db:
            head = db.execute(
                "SELECT seq FROM heads WHERE shard=?", (shard,)
            ).fetchone()[0]
            if after > head:
                raise Error(409, "projection cursor ahead of authority")
            return {
                "lineage": self.lineage,
                "shard": shard,
                "head": head,
                "after": after,
                "events": [
                    {
                        "seq": r["seq"],
                        "payload": json.loads(r["payload"]),
                        "hash": r["hash"],
                    }
                    for r in db.execute(
                        "SELECT * FROM events WHERE shard=? AND seq>? ORDER BY seq LIMIT 200",
                        (shard, after),
                    )
                ],
            }

    def rows(self, month):
        self.month(month)
        with self.transaction() as db:
            return [
                {"user": r["user"], "score": r["score"], "name": r["name"]}
                for r in db.execute(
                    "SELECT s.user,s.score,p.name FROM scores s JOIN players p ON s.user=p.id WHERE month=? ORDER BY s.score DESC,s.user",
                    (month,),
                )
            ]

    def names(self, users):
        with self.transaction() as db:
            return {
                user: db.execute(
                    "SELECT name FROM players WHERE id=?", (user,)
                ).fetchone()[0]
                for user in users
            }

    def close(self, month):
        month_bounds(month)
        with self.transaction(True) as db:
            row = db.execute("SELECT * FROM seasons WHERE month=?", (month,)).fetchone()
            if not row:
                raise Error(404, "season missing")
            if row["settlement"]:
                return json.loads(row["settlement"])
            if self.clock() < month_bounds(month)[1] + self.grace:
                raise Error(409, "late acceptance period is not over")
            rows = []
            rank, previous = 0, None
            for position, value in enumerate(
                db.execute(
                    "SELECT user,score FROM scores WHERE month=? ORDER BY score DESC,user",
                    (month,),
                ),
                1,
            ):
                if value["score"] != previous:
                    rank = position
                rows.append(
                    {
                        "user": value["user"],
                        "score": value["score"],
                        "rank": rank,
                        "position": position,
                    }
                )
                previous = value["score"]
            snapshot = {
                "month": month,
                "players": len(rows),
                "sha256": digest(rows),
                "top10": rows[:10],
                "closed_at": int(self.clock()),
            }
            db.execute(
                "UPDATE seasons SET status='closed',settlement=? WHERE month=?",
                (dumps(snapshot), month),
            )
            return snapshot
