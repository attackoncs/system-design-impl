"""Partition-local aggregation: input positions and output commit together."""

import hashlib
import time

from .aggregation_schema import initialize
from .common import Database, Error, dumps, integer, query
from .records import (
    DEFAULT_DEDUP_HORIZON,
    DEFAULT_REPLAY_HORIZON,
    dedup_horizon as checked_dedup_horizon,
    expired,
    prepare_batch,
    replay_horizon as checked_replay_horizon,
    timestamp,
)

# Upper bound on rows deleted per table per apply: amortized GC that outpaces
# the at most 200 rows a page can add, without long write transactions.
GC_BATCH = 512


class Aggregator(Database):
    def __init__(
        self,
        path,
        partition,
        lateness=120,
        clock=time.time,
        *,
        dedup_horizon=DEFAULT_DEDUP_HORIZON,
        replay_horizon=DEFAULT_REPLAY_HORIZON,
    ):
        integer(partition, "partition", 0, 63)
        integer(lateness, "lateness", 0, 86400)
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.dedup_horizon = checked_dedup_horizon(dedup_horizon, lateness)
        self.replay_horizon = checked_replay_horizon(replay_horizon)
        super().__init__(path)
        self.partition, self.lateness, self.clock = partition, lateness, clock
        initialize(self, partition)

    def apply(self, batch, before_commit=None):
        """Freeze/verify CPU work first, then atomically accept a contiguous page.

        Old positions are validated too. Full offset receipts cover positions
        accepted since migration and inside the replay horizon; older legacy
        receipts fall back to stored event IDs, and positions whose receipts
        were garbage-collected fail explicitly. New events older than the dedup
        horizon are counted as expired, never added to the buckets.
        """
        batch = prepare_batch(batch, self.partition)
        with self.transaction(True) as db:
            cp = dict(db.execute("SELECT * FROM checkpoint").fetchone())
            if cp["lineage"] and cp["lineage"] != batch["lineage"]:
                raise Error(409, "queue lineage changed; explicit rebuild required")
            cp["lineage"] = batch["lineage"]
            meta = dict(db.execute("SELECT key,value FROM worker_meta"))
            floor, replay_floor = meta["position_floor"], meta["replay_floor"]
            for entry in batch["entries"]:
                value, fingerprint, seq = entry["event"], entry["hash"], entry["seq"]
                # Decided before any ID lookup so the outcome never depends on
                # how far GC has progressed (rebuild and reconcile agree).
                too_old = seq > cp["seq"] and expired(
                    value["click_timestamp"], cp["max_event"], self.dedup_horizon
                )
                seen = None
                if not too_old:
                    seen = db.execute(
                        "SELECT hash FROM seen WHERE event_id=?", (value["event_id"],)
                    ).fetchone()
                if seen and seen[0] != fingerprint:
                    raise Error(409, "conflicting replay event")
                if seq <= cp["seq"]:
                    if seq < replay_floor:
                        raise Error(
                            409, "source position is older than the replay horizon"
                        )
                    receipt = db.execute(
                        "SELECT event_id,hash,accepted FROM input_positions WHERE seq=?",
                        (seq,),
                    ).fetchone()
                    if receipt:
                        if tuple(receipt) != (
                            value["event_id"],
                            fingerprint,
                            entry["accepted"],
                        ):
                            raise Error(409, "source position content changed")
                    elif seq >= floor or not seen:
                        raise Error(
                            409, "historical source position cannot be verified"
                        )
                    continue
                if seq != cp["seq"] + 1:
                    raise Error(409, "input offset gap")
                minute = value["click_timestamp"] // 60 * 60
                if too_old:
                    cp["expired"] += 1
                elif not seen:
                    late = minute + 60 <= cp["watermark"]
                    db.execute(
                        "INSERT INTO seen(event_id,hash,click_timestamp) VALUES(?,?,?)",
                        (value["event_id"], fingerprint, value["click_timestamp"]),
                    )
                    db.execute(
                        """INSERT INTO buckets VALUES(?,?,?,?,?,1,1)
                    ON CONFLICT(minute,ad_id,user_id,ip,country) DO UPDATE SET count=count+1,revision=revision+1""",
                        (
                            minute,
                            value["ad_id"],
                            value["user_id"],
                            value["ip"],
                            value["country"],
                        ),
                    )
                    cp["late"] += int(late)
                db.execute(
                    "INSERT INTO input_positions VALUES(?,?,?,?)",
                    (seq, value["event_id"], fingerprint, entry["accepted"]),
                )
                cp["seq"] = seq
                cp["max_event"] = max(cp["max_event"], value["click_timestamp"])
                cp["watermark"] = max(
                    cp["watermark"],
                    cp["max_event"] - self.lateness,
                    entry["accepted"] - self.lateness,
                )
            db.execute(
                "UPDATE checkpoint SET lineage=?,seq=?,watermark=?,late=?,max_event=?,expired=? WHERE id=1",
                (
                    cp["lineage"],
                    cp["seq"],
                    cp["watermark"],
                    cp["late"],
                    cp["max_event"],
                    cp["expired"],
                ),
            )
            self._collect(db, cp, replay_floor)
            if before_commit:
                before_commit()
        cp.pop("id")
        return cp

    def _collect(self, db, cp, replay_floor):
        """Bounded, index-range state GC inside the caller's apply transaction."""
        if self.dedup_horizon is not None:
            # Every later position has max_event >= cp["max_event"], so an ID
            # below this cutoff can only arrive again as an expired event.
            # NULL (pre-migration) times never compare below and are kept.
            db.execute(
                "DELETE FROM seen WHERE rowid IN (SELECT rowid FROM seen"
                " WHERE click_timestamp<? ORDER BY click_timestamp LIMIT ?)",
                (cp["max_event"] - self.dedup_horizon, GC_BATCH),
            )
        if self.replay_horizon is not None:
            # Raise the floor first: a position without a retained receipt is
            # rejected, never re-verified by event ID or counted again.
            target = cp["seq"] - self.replay_horizon + 1
            if target > replay_floor:
                db.execute(
                    "UPDATE worker_meta SET value=? WHERE key='replay_floor'",
                    (target,),
                )
                replay_floor = target
            db.execute(
                "DELETE FROM input_positions WHERE seq IN (SELECT seq FROM"
                " input_positions WHERE seq<? ORDER BY seq LIMIT ?)",
                (replay_floor, GC_BATCH),
            )

    def idle(self):
        with self.transaction(True) as db:
            now = timestamp(self.clock(), "clock")
            db.execute(
                "UPDATE checkpoint SET watermark=max(watermark,?) WHERE lineage<>''",
                (max(0, now - self.lateness),),
            )

    def status(self, counts=True):
        """Checkpoint row; counts=False skips the O(buckets) scan for hot polls."""
        with self.transaction() as db:
            result = dict(db.execute("SELECT * FROM checkpoint").fetchone())
            result.pop("id")
            if counts:
                result["bucket_rows"] = db.execute(
                    "SELECT count(*) FROM buckets"
                ).fetchone()[0]
            return result

    def totals(self, body):
        q = query(body, timestamp(self.clock(), "clock"))
        where = ["minute>=?", "minute<?"]
        args = [q["from"], q["to"]]
        for key, value in {
            **q["filters"],
            **({"ad_id": q["ad_id"]} if "ad_id" in q else {}),
        }.items():
            where.append(key + "=?")
            args.append(value)
        with self.transaction() as db:
            rows = db.execute(
                "SELECT ad_id,sum(count) FROM buckets WHERE "
                + " AND ".join(where)
                + " GROUP BY ad_id ORDER BY ad_id LIMIT 10001",
                args,
            ).fetchall()
            if len(rows) > 10000:
                raise Error(422, "too many candidate ads; narrow range or filters")
            cp = dict(db.execute("SELECT * FROM checkpoint").fetchone())
            return {
                "totals": dict(rows),
                "checkpoint": cp,
                "closed": q["to"] <= cp["watermark"],
            }

    def audit(self, max_rows=1_000_000):
        """Hash one consistent snapshot without retaining every dimension row."""
        integer(max_rows, "audit row budget", 1, 1_000_000)
        hasher = hashlib.sha256()
        hasher.update(b"[")
        count = total = 0
        with self.transaction() as db:
            cp = dict(db.execute("SELECT * FROM checkpoint").fetchone())
            rows = db.execute(
                "SELECT minute,ad_id,user_id,ip,country,count FROM buckets ORDER BY minute,ad_id,user_id,ip,country LIMIT ?",
                (max_rows + 1,),
            )
            for row in rows:
                if count >= max_rows:
                    raise Error(413, "audit row budget exceeded")
                if count:
                    hasher.update(b",")
                hasher.update(dumps(list(row)).encode())
                count += 1
                total += row[-1]
        hasher.update(b"]")
        return {
            "checkpoint": cp,
            "digest": hasher.hexdigest(),
            "bucket_rows": count,
            "count": total,
        }
