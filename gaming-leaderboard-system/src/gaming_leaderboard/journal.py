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
from .journal_records import (
    MAX_PLAYERS,
    SCHEMA,
    display_name,
    event_payload,
    match_result,
    timestamp,
)


class Journal(Database):
    def __init__(self, path, shards=2, grace=172800, clock=time.time):
        super().__init__(path)
        self.shards, self.grace, self.clock = shards, grace, clock
        integer(shards, "shards", 1, 16)
        integer(grace, "grace", 0, 604800)
        self.initialize(SCHEMA)
        with self.transaction(True) as db:
            if "profile_revision" not in {
                row["name"] for row in db.execute("PRAGMA table_info(metadata)")
            }:
                db.execute(
                    "ALTER TABLE metadata ADD COLUMN profile_revision INTEGER NOT NULL DEFAULT 0"
                )
            db.execute(
                "INSERT OR IGNORE INTO metadata(id,lineage,shards,grace) VALUES(1,?,?,?)",
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
        self._emit_many(db, [{"month": month, "user": user, "score": score}])

    def _emit_many(self, db, values):
        heads = {row["shard"]: row["seq"] for row in db.execute("SELECT * FROM heads")}
        records, changed = [], set()
        for value in values:
            payload = event_payload(value)
            shard = shard_for(payload["user"], self.shards)
            heads[shard] += 1
            changed.add(shard)
            records.append((shard, heads[shard], dumps(payload), digest(payload)))
        db.executemany("INSERT INTO events VALUES(?,?,?,?)", records)
        db.executemany(
            "UPDATE heads SET seq=? WHERE shard=?",
            [(heads[shard], shard) for shard in sorted(changed)],
        )

    def ensure(self, db, month):
        _, end = month_bounds(month)
        db.execute("INSERT OR IGNORE INTO seasons VALUES(?,'open',NULL)", (month,))
        if (
            db.execute("SELECT status FROM seasons WHERE month=?", (month,)).fetchone()[
                0
            ]
            != "open"
        ):
            return
        missing = [
            row[0]
            for row in db.execute(
                """
            SELECT p.id FROM players p LEFT JOIN scores s ON s.month=? AND s.user=p.id
            WHERE p.registered<? AND s.user IS NULL ORDER BY p.id LIMIT ?
        """,
                (month, end, MAX_PLAYERS + 1),
            )
        ]
        if len(missing) > MAX_PLAYERS:
            raise Error(422, "season exceeds reference player budget")
        db.executemany(
            "INSERT INTO scores VALUES(?,?,0)", [(month, user) for user in missing]
        )
        self._emit_many(
            db, [{"month": month, "user": user, "score": 0} for user in missing]
        )

    def register(self, user, name):
        identifier(user)
        display_name(name)
        with self.transaction(True) as db:
            now = timestamp(self.clock)
            old = db.execute("SELECT name FROM players WHERE id=?", (user,)).fetchone()
            if (
                not old
                and db.execute("SELECT count(*) FROM players").fetchone()[0]
                >= MAX_PLAYERS
            ):
                raise Error(429, "reference player capacity reached")
            db.execute(
                "INSERT INTO players VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                (user, name, int(now)),
            )
            if old is None or old["name"] != name:
                db.execute(
                    "UPDATE metadata SET profile_revision=profile_revision+1 WHERE id=1"
                )
            month = current_month(now)
            season = db.execute(
                "SELECT status FROM seasons WHERE month=?", (month,)
            ).fetchone()
            if season is None:
                self.ensure(db, month)
            elif (
                season["status"] == "open"
                and db.execute(
                    "INSERT OR IGNORE INTO scores VALUES(?,?,0)", (month, user)
                ).rowcount
            ):
                self.emit(db, month, user, 0)
        return {"user": user, "name": name}

    def month(self, month=None):
        return self.snapshot(month)["season"]

    def snapshot(self, month=None):
        if month is not None:
            month_bounds(month)
        with self.transaction(True) as db:
            now = timestamp(self.clock)
            month = current_month(now) if month is None else month
            row = db.execute("SELECT * FROM seasons WHERE month=?", (month,)).fetchone()
            if row is None and month == current_month(now):
                self.ensure(db, month)
                row = db.execute(
                    "SELECT * FROM seasons WHERE month=?", (month,)
                ).fetchone()
            if not row:
                raise Error(404, "season missing")
            return self._snapshot_record(db, row)

    def read_snapshot(self, month=None):
        """Return a consistent read-only watermark, or None for preparation."""
        if month is not None:
            month_bounds(month)
        with self.transaction() as db:
            month = current_month(timestamp(self.clock)) if month is None else month
            row = db.execute("SELECT * FROM seasons WHERE month=?", (month,)).fetchone()
            return None if row is None else self._snapshot_record(db, row)

    def _snapshot_record(self, db, row):
        result = dict(row)
        result["settlement"] = (
            json.loads(result["settlement"]) if result["settlement"] else None
        )
        result["accept_until"] = month_bounds(result["month"])[1] + self.grace
        return {
            "season": result,
            "lineage": self.lineage,
            "heads": {str(r[0]): r[1] for r in db.execute("SELECT * FROM heads")},
            "profile_revision": db.execute(
                "SELECT profile_revision FROM metadata WHERE id=1"
            ).fetchone()[0],
        }

    def ingest(self, body, before_commit=None):
        normalized = match_result(body)
        match, winner = normalized["match_id"], normalized["winner"]
        players, ended_at = normalized["players"], normalized["ended_at"]
        fingerprint = digest(normalized)
        with self.transaction(True) as db:
            old = db.execute("SELECT * FROM matches WHERE id=?", (match,)).fetchone()
            if old:
                if old["hash"] != fingerprint:
                    raise Error(409, "match ID reused with different result")
                return {**json.loads(old["receipt"]), "duplicate": True}
            now = timestamp(self.clock)
            if ended_at > now + 30:
                raise ValueError("match is too far in the future")
            month = current_month(ended_at)
            if now >= month_bounds(month)[1] + self.grace:
                raise Error(409, "late acceptance deadline has passed")
            if month > current_month(now):
                raise ValueError("future-month match is not accepted")
            for user in players:
                row = db.execute(
                    "SELECT registered FROM players WHERE id=?", (user,)
                ).fetchone()
                if not row or row[0] > ended_at:
                    raise Error(
                        409, "participant was not registered when the match ended"
                    )
            season = db.execute(
                "SELECT status FROM seasons WHERE month=?", (month,)
            ).fetchone()
            if season is None:
                self.ensure(db, month)
                season = db.execute(
                    "SELECT status FROM seasons WHERE month=?", (month,)
                ).fetchone()
            if season["status"] != "open":
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
                "profile_revision": db.execute(
                    "SELECT profile_revision FROM metadata WHERE id=1"
                ).fetchone()[0],
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
            rows = db.execute(
                "SELECT * FROM events WHERE shard=? AND seq>? ORDER BY seq LIMIT 200",
                (shard, after),
            ).fetchall()
            events = []
            for expected, row in enumerate(rows, after + 1):
                try:
                    payload = event_payload(json.loads(row["payload"]))
                    if (
                        row["seq"] != expected
                        or shard_for(payload["user"], self.shards) != shard
                        or digest(payload) != row["hash"]
                    ):
                        raise ValueError("event identity/checksum/sequence mismatch")
                except (ValueError, TypeError, KeyError):
                    raise Error(409, "invalid authoritative event history") from None
                events.append(
                    {"seq": row["seq"], "payload": payload, "hash": row["hash"]}
                )
            if len(events) != min(200, head - after):
                raise Error(409, "authoritative event history has a gap")
            return {
                "lineage": self.lineage,
                "shard": shard,
                "head": head,
                "after": after,
                "events": events,
            }

    def rows(self, month):
        self.month(month)
        with self.transaction() as db:
            rows = [
                {"user": r["user"], "score": r["score"], "name": r["name"]}
                for r in db.execute(
                    "SELECT s.user,s.score,p.name FROM scores s JOIN players p ON s.user=p.id WHERE month=? ORDER BY s.score DESC,s.user LIMIT 10001",
                    (month,),
                )
            ]
            if len(rows) > MAX_PLAYERS:
                raise Error(422, "season exceeds reference player budget")
            return rows

    def names(self, users):
        if not isinstance(users, (list, tuple)):
            raise ValueError("names requires at most 10000 identifiers")
        users = tuple(users)
        if len(users) > MAX_PLAYERS:
            raise ValueError("names requires at most 10000 identifiers")
        users = tuple(dict.fromkeys(identifier(user) for user in users))
        with self.transaction() as db:
            names = {}
            for offset in range(0, len(users), 500):
                part = users[offset : offset + 500]
                names.update(
                    {
                        row["id"]: row["name"]
                        for row in db.execute(
                            "SELECT id,name FROM players WHERE id IN ("
                            + ",".join("?" for _ in part)
                            + ")",
                            part,
                        )
                    }
                )
            if len(names) != len(users):
                raise Error(404, "player missing")
            return names

    def close(self, month):
        month_bounds(month)
        with self.transaction(True) as db:
            row = db.execute("SELECT * FROM seasons WHERE month=?", (month,)).fetchone()
            if not row:
                raise Error(404, "season missing")
            if row["settlement"]:
                return json.loads(row["settlement"])
            now = timestamp(self.clock)
            if now < month_bounds(month)[1] + self.grace:
                raise Error(409, "late acceptance period is not over")
            rows = []
            rank, previous = 0, None
            for position, value in enumerate(
                db.execute(
                    "SELECT user,score FROM scores WHERE month=? ORDER BY score DESC,user LIMIT 10001",
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
            if len(rows) > MAX_PLAYERS:
                raise Error(422, "season exceeds reference player budget")
            snapshot = {
                "month": month,
                "players": len(rows),
                "sha256": digest(rows),
                "top10": rows[:10],
                "closed_at": int(now),
            }
            db.execute(
                "UPDATE seasons SET status='closed',settlement=? WHERE month=?",
                (dumps(snapshot), month),
            )
            return snapshot
