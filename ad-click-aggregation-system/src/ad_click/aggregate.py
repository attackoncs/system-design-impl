"""Partition-local aggregation: input positions and output commit together."""

import hashlib
import time

from .aggregation_schema import initialize
from .common import Database, Error, dumps, integer, query
from .records import prepare_batch, timestamp


class Aggregator(Database):
    def __init__(self, path, partition, lateness=120, clock=time.time):
        integer(partition, "partition", 0, 63)
        integer(lateness, "lateness", 0, 86400)
        if not callable(clock):
            raise ValueError("clock must be callable")
        super().__init__(path)
        self.partition, self.lateness, self.clock = partition, lateness, clock
        initialize(self, partition)

    def apply(self, batch, before_commit=None):
        """Freeze/verify CPU work first, then atomically accept a contiguous page.

        Old positions are validated too. Full offset receipts cover all positions
        accepted since migration; older receipts fall back to stored event IDs.
        """
        batch = prepare_batch(batch, self.partition)
        with self.transaction(True) as db:
            cp = dict(db.execute("SELECT * FROM checkpoint").fetchone())
            if cp["lineage"] and cp["lineage"] != batch["lineage"]:
                raise Error(409, "queue lineage changed; explicit rebuild required")
            cp["lineage"] = batch["lineage"]
            floor = db.execute(
                "SELECT value FROM worker_meta WHERE key='position_floor'"
            ).fetchone()[0]
            for entry in batch["entries"]:
                value, fingerprint, seq = entry["event"], entry["hash"], entry["seq"]
                seen = db.execute(
                    "SELECT hash FROM seen WHERE event_id=?", (value["event_id"],)
                ).fetchone()
                if seen and seen[0] != fingerprint:
                    raise Error(409, "conflicting replay event")
                if seq <= cp["seq"]:
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
                if not seen:
                    late = minute + 60 <= cp["watermark"]
                    db.execute(
                        "INSERT INTO seen VALUES(?,?)", (value["event_id"], fingerprint)
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
                "UPDATE checkpoint SET lineage=?,seq=?,watermark=?,late=?,max_event=? WHERE id=1",
                (
                    cp["lineage"],
                    cp["seq"],
                    cp["watermark"],
                    cp["late"],
                    cp["max_event"],
                ),
            )
            if before_commit:
                before_commit()
        return self.status()

    def idle(self):
        with self.transaction(True) as db:
            now = timestamp(self.clock(), "clock")
            db.execute(
                "UPDATE checkpoint SET watermark=max(watermark,?) WHERE lineage<>''",
                (max(0, now - self.lateness),),
            )

    def status(self):
        with self.transaction() as db:
            result = dict(db.execute("SELECT * FROM checkpoint").fetchone())
            result.pop("id")
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
