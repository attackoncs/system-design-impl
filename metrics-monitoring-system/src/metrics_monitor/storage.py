import json
import math
import threading
import time

from .common import (
    Database,
    Error,
    digest,
    dumps,
    identifier,
    number,
    sample,
)
from .cache import Cache
from .storage_schema import initialize
from .rollups import Compaction
from .queries import Queries
from .archives import Archives, read_archive as read_archive
from .summaries import combine as combine, reading as reading, singleton as singleton

DAY = 86400


class Store(Archives, Queries, Compaction, Database):
    def __init__(self, path, clock=time.time, max_series=10000, per_metric=1000):
        if not callable(clock):
            raise ValueError("clock must be callable")
        super().__init__(path)
        self.clock, self.cache = clock, Cache()
        self._compaction = threading.Lock()
        initialize(self, max_series, per_metric)

    def meta(self, db):
        return dict(db.execute("SELECT key,value FROM meta"))

    def _now(self):
        try:
            value = number(self.clock())
        except OverflowError:
            raise ValueError("invalid clock") from None
        if value < 0:
            raise ValueError("invalid clock")
        return value

    def cutoffs(self, now=None):
        now = self._now() if now is None else now
        return (
            math.floor((now - 7 * DAY) / 60) * 60,
            math.floor((now - 30 * DAY) / 3600) * 3600,
            math.floor((now - 365 * DAY) / 3600) * 3600,
        )

    def ingest(self, batch_id, samples):
        identifier(batch_id)
        if not isinstance(samples, list) or not 1 <= len(samples) <= 100:
            raise ValueError("batch requires 1..100 samples")
        # Preserve the historical raw request fingerprint while owning its exact
        # content before waiting on a SQLite writer.
        captured = json.loads(dumps(samples))
        fingerprint = digest(captured)
        normalized = [sample(body) for body in captured]
        with self.transaction(True) as db:
            old = db.execute(
                "SELECT hash,result FROM batches WHERE id=?", (batch_id,)
            ).fetchone()
            if old:
                if old["hash"] != fingerprint:
                    raise Error(409, "batch ID payload conflict")
                return json.loads(old["result"])
            meta = self.meta(db)
            now = self._now()
            raw_floor, _, _ = self.cutoffs(now)
            total = db.execute("SELECT count(*) FROM series").fetchone()[0]
            families = {}
            added = 0
            for item in normalized:
                if not raw_floor <= item["timestamp"] <= now + 30:
                    raise ValueError("sample outside raw retention/future tolerance")
                sid = digest([item["metric"], item["labels"]])
                kind = db.execute(
                    "SELECT kind FROM kinds WHERE metric=?", (item["metric"],)
                ).fetchone()
                if kind and kind[0] != item["type"]:
                    raise Error(409, "metric family type conflict")
                db.execute(
                    "INSERT OR IGNORE INTO kinds VALUES(?,?)",
                    (item["metric"], item["type"]),
                )
                exists = db.execute(
                    "SELECT 1 FROM series WHERE id=?", (sid,)
                ).fetchone()
                if not exists:
                    if item["metric"] not in families:
                        families[item["metric"]] = db.execute(
                            "SELECT count(*) FROM series WHERE metric=?",
                            (item["metric"],),
                        ).fetchone()[0]
                    family = families[item["metric"]]
                    if total >= meta["max_series"] or family >= meta["per_metric"]:
                        raise Error(429, "time-series cardinality budget exceeded")
                    db.execute(
                        "INSERT INTO series VALUES(?,?,?,?)",
                        (sid, item["metric"], dumps(item["labels"]), item["type"]),
                    )
                    db.executemany(
                        "INSERT INTO tags VALUES(?,?,?)",
                        [(sid, key, value) for key, value in item["labels"].items()],
                    )
                    total += 1
                    families[item["metric"]] += 1
                previous = db.execute(
                    "SELECT value FROM raw WHERE series=? AND timestamp=?",
                    (sid, item["timestamp"]),
                ).fetchone()
                if previous:
                    if previous[0] != item["value"]:
                        raise Error(409, "immutable sample value conflict")
                else:
                    db.execute(
                        "INSERT INTO raw VALUES(?,?,?)",
                        (sid, item["timestamp"], item["value"]),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO dirty_minutes VALUES(?,?)",
                        (sid, math.floor(item["timestamp"] / 60) * 60),
                    )
                    added += 1
            if added:
                db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")
                db.execute("UPDATE meta SET value=-1 WHERE key='compact_minute'")
            result = {
                "batch_id": batch_id,
                "samples": len(normalized),
                "new_samples": added,
            }
            db.execute(
                "INSERT INTO batches VALUES(?,?,?,?)",
                (batch_id, fingerprint, dumps(result), now),
            )
            return result

    def reject(self, batch_id, payload, reason):
        with self.transaction(True) as db:
            db.execute(
                "INSERT OR IGNORE INTO rejections VALUES(?,?,?,?)",
                (batch_id, digest(payload), str(reason)[:500], self.clock()),
            )

    def health(self):
        with self.transaction() as db:
            return {
                "series": db.execute("SELECT count(*) FROM series").fetchone()[0],
                "raw_samples": db.execute("SELECT count(*) FROM raw").fetchone()[0],
                "rollup_buckets": db.execute("SELECT count(*) FROM rollups").fetchone()[
                    0
                ],
                "rejected_batches": db.execute(
                    "SELECT count(*) FROM rejections"
                ).fetchone()[0],
                "revision": self.meta(db)["revision"],
                "workers": [dict(r) for r in db.execute("SELECT * FROM workers")],
            }
