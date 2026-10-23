import time

from .common import Database, Error, digest, event, integer, query


class Aggregator(Database):
    def __init__(self, path, partition, lateness=120, clock=time.time):
        super().__init__(path)
        self.partition, self.lateness, self.clock = partition, lateness, clock
        integer(partition, "partition", 0, 63)
        integer(lateness, "lateness", 0, 86400)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS checkpoint(id INTEGER PRIMARY KEY CHECK(id=1), partition INTEGER,
            lineage TEXT, seq INTEGER, watermark REAL, late INTEGER, max_event REAL);
        CREATE TABLE IF NOT EXISTS seen(event_id TEXT PRIMARY KEY, hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS buckets(minute INTEGER, ad_id TEXT, user_id TEXT, ip TEXT, country TEXT,
            count INTEGER NOT NULL, revision INTEGER NOT NULL,
            PRIMARY KEY(minute,ad_id,user_id,ip,country));
        CREATE INDEX IF NOT EXISTS by_ad ON buckets(ad_id,minute);
        CREATE INDEX IF NOT EXISTS by_country ON buckets(country,minute);
        CREATE INDEX IF NOT EXISTS by_user ON buckets(user_id,minute);
        CREATE INDEX IF NOT EXISTS by_ip ON buckets(ip,minute);
        """)
        with self.transaction(True) as db:
            db.execute(
                "INSERT OR IGNORE INTO checkpoint VALUES(1,?,'',0,0,0,0)", (partition,)
            )
            if (
                db.execute("SELECT partition FROM checkpoint").fetchone()[0]
                != partition
            ):
                raise Error(409, "worker database belongs to another partition")

    def apply(self, batch, before_commit=None):
        """Output, dedup keys and input position commit in one SQLite transaction."""
        if batch["partition"] != self.partition:
            raise Error(409, "wrong partition")
        with self.transaction(True) as db:
            cp = dict(db.execute("SELECT * FROM checkpoint").fetchone())
            if cp["lineage"] and cp["lineage"] != batch["lineage"]:
                raise Error(409, "queue lineage changed; explicit rebuild required")
            cp["lineage"] = batch["lineage"]
            for entry in batch["entries"]:
                if entry["seq"] <= cp["seq"]:
                    continue
                if entry["seq"] != cp["seq"] + 1:
                    raise Error(409, "input offset gap")
                value = event(entry["event"], entry["accepted"])
                fingerprint = digest(value)
                if fingerprint != entry["hash"]:
                    raise Error(409, "raw log checksum mismatch")
                seen = db.execute(
                    "SELECT hash FROM seen WHERE event_id=?", (value["event_id"],)
                ).fetchone()
                if seen and seen[0] != fingerprint:
                    raise Error(409, "conflicting replay event")
                minute = value["click_timestamp"] // 60 * 60
                if not seen:
                    late = minute + 60 <= cp["watermark"]
                    db.execute(
                        "INSERT INTO seen VALUES(?,?)", (value["event_id"], fingerprint)
                    )
                    db.execute(
                        """INSERT INTO buckets VALUES(?,?,?,?,?,1,1)
                        ON CONFLICT(minute,ad_id,user_id,ip,country) DO UPDATE SET
                        count=count+1,revision=revision+1""",
                        (
                            minute,
                            value["ad_id"],
                            value["user_id"],
                            value["ip"],
                            value["country"],
                        ),
                    )
                    cp["late"] += int(late)
                cp["seq"] = entry["seq"]
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
            db.execute(
                "UPDATE checkpoint SET watermark=max(watermark,?)",
                (self.clock() - self.lateness,),
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
        q = query(body, self.clock())
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

    def audit(self):
        # Hash dimensions as well as totals: equal grand totals can hide allocation bugs.
        with self.transaction() as db:
            cp = dict(db.execute("SELECT * FROM checkpoint").fetchone())
            rows = [
                list(r)
                for r in db.execute(
                    "SELECT minute,ad_id,user_id,ip,country,count FROM buckets ORDER BY minute,ad_id,user_id,ip,country"
                )
            ]
            return {
                "checkpoint": cp,
                "digest": digest(rows),
                "bucket_rows": len(rows),
                "count": sum(r[-1] for r in rows),
            }
