from contextlib import closing
import json
import gzip
import math
import time

from .common import (
    Cache,
    Database,
    Error,
    digest,
    dumps,
    identifier,
    labels,
    number,
    sample,
)

DAY = 86400


def singleton(timestamp, value):
    return {
        "count": 1,
        "sum": value,
        "min": value,
        "max": value,
        "first_t": timestamp,
        "first": value,
        "last_t": timestamp,
        "last": value,
        "increase": 0.0,
    }


def combine(values, kind):
    ordered = sorted(values, key=lambda value: value["first_t"])
    if not ordered:
        return None
    result = dict(ordered[0])
    for item in ordered[1:]:
        boundary = (
            item["first"] - result["last"]
            if item["first"] >= result["last"]
            else item["first"]
        )
        result["increase"] += item["increase"] + (boundary if kind == "counter" else 0)
        result["count"] += item["count"]
        result["sum"] += item["sum"]
        result["min"] = min(result["min"], item["min"])
        result["max"] = max(result["max"], item["max"])
        result["last_t"], result["last"] = item["last_t"], item["last"]
    return result


def reading(stats, aggregation, kind):
    if not stats:
        return None
    if aggregation == "last":
        return stats["last"]
    if kind == "gauge" and aggregation in ("avg", "min", "max"):
        return (
            stats["sum"] / stats["count"]
            if aggregation == "avg"
            else stats[aggregation]
        )
    if kind == "counter" and aggregation in ("increase", "rate"):
        return (
            stats["increase"]
            if aggregation == "increase"
            else stats["increase"] / (stats["last_t"] - stats["first_t"])
            if stats["last_t"] > stats["first_t"]
            else None
        )
    raise ValueError("aggregation incompatible with metric type")


class Store(Database):
    def __init__(self, path, clock=time.time, max_series=10000, per_metric=1000):
        super().__init__(path)
        self.clock, self.cache = clock, Cache()
        with closing(self.connect()) as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value INTEGER);
            CREATE TABLE IF NOT EXISTS kinds(metric TEXT PRIMARY KEY,kind TEXT);
            CREATE TABLE IF NOT EXISTS series(id TEXT PRIMARY KEY,metric TEXT,labels TEXT,kind TEXT);
            CREATE INDEX IF NOT EXISTS series_metric ON series(metric);
            CREATE TABLE IF NOT EXISTS tags(series TEXT,key TEXT,value TEXT,PRIMARY KEY(series,key));
            CREATE INDEX IF NOT EXISTS tags_lookup ON tags(key,value,series);
            CREATE TABLE IF NOT EXISTS raw(series TEXT,timestamp REAL,value REAL,PRIMARY KEY(series,timestamp));
            CREATE TABLE IF NOT EXISTS rollups(series TEXT,grain INTEGER,bucket INTEGER,stats TEXT,PRIMARY KEY(series,grain,bucket));
            CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,hash TEXT,result TEXT,received REAL);
            CREATE TABLE IF NOT EXISTS rejections(id TEXT PRIMARY KEY,hash TEXT,reason TEXT,received REAL);
            CREATE TABLE IF NOT EXISTS workers(id TEXT PRIMARY KEY,updated REAL,error TEXT);
            """)
            for key, value in {
                "revision": 0,
                "compact_minute": -1,
                "max_series": max_series,
                "per_metric": per_metric,
            }.items():
                if (
                    type(value) is not int
                    or key in ("max_series", "per_metric")
                    and value < 1
                ):
                    raise ValueError("invalid cardinality budget")
                db.execute("INSERT OR IGNORE INTO meta VALUES(?,?)", (key, value))
            db.commit()

    def meta(self, db):
        return dict(db.execute("SELECT key,value FROM meta"))

    def cutoffs(self):
        now = self.clock()
        return (
            math.floor((now - 7 * DAY) / 60) * 60,
            math.floor((now - 30 * DAY) / 3600) * 3600,
            math.floor((now - 365 * DAY) / 3600) * 3600,
        )

    def ingest(self, batch_id, samples):
        identifier(batch_id)
        if not isinstance(samples, list) or not 1 <= len(samples) <= 100:
            raise ValueError("batch requires 1..100 samples")
        fingerprint = digest(samples)
        with self.transaction(True) as db:
            old = db.execute(
                "SELECT hash,result FROM batches WHERE id=?", (batch_id,)
            ).fetchone()
            if old:
                if old["hash"] != fingerprint:
                    raise Error(409, "batch ID payload conflict")
                return json.loads(old["result"])
            meta = self.meta(db)
            raw_floor, _, _ = self.cutoffs()
            added = 0
            for body in samples:
                item = sample(body)
                if not raw_floor <= item["timestamp"] <= self.clock() + 30:
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
                    total = db.execute("SELECT count(*) FROM series").fetchone()[0]
                    family = db.execute(
                        "SELECT count(*) FROM series WHERE metric=?", (item["metric"],)
                    ).fetchone()[0]
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
                    added += 1
            if added:
                db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")
                db.execute("UPDATE meta SET value=-1 WHERE key='compact_minute'")
            result = {
                "batch_id": batch_id,
                "samples": len(samples),
                "new_samples": added,
            }
            db.execute(
                "INSERT INTO batches VALUES(?,?,?,?)",
                (batch_id, fingerprint, dumps(result), self.clock()),
            )
            return result

    def reject(self, batch_id, payload, reason):
        with self.transaction(True) as db:
            db.execute(
                "INSERT OR IGNORE INTO rejections VALUES(?,?,?,?)",
                (batch_id, digest(payload), str(reason)[:500], self.clock()),
            )

    def compact(self):
        current_minute = int(self.clock() // 60)
        with self.transaction() as db:
            if self.meta(db)["compact_minute"] == current_minute:
                return
        with self.transaction(True) as db:
            kinds = {
                r["id"]: r["kind"] for r in db.execute("SELECT id,kind FROM series")
            }
            minutes = {}
            watermark = math.floor((self.clock() - 120) / 60) * 60
            for row in db.execute(
                "SELECT * FROM raw WHERE timestamp<? ORDER BY series,timestamp",
                (watermark,),
            ):
                key = (row["series"], int(row["timestamp"] // 60) * 60)
                minutes.setdefault(key, []).append(
                    singleton(row["timestamp"], row["value"])
                )
            for (sid, bucket), values in minutes.items():
                db.execute(
                    "INSERT INTO rollups VALUES(?,60,?,?) ON CONFLICT(series,grain,bucket) DO UPDATE SET stats=excluded.stats",
                    (sid, bucket, dumps(combine(values, kinds[sid]))),
                )
            hours = {}
            closed_hour = math.floor((self.clock() - 120) / 3600) * 3600
            for row in db.execute(
                "SELECT * FROM rollups WHERE grain=60 AND bucket<? ORDER BY series,bucket",
                (closed_hour,),
            ):
                key = (row["series"], row["bucket"] // 3600 * 3600)
                hours.setdefault(key, []).append(json.loads(row["stats"]))
            for (sid, bucket), values in hours.items():
                db.execute(
                    "INSERT INTO rollups VALUES(?,3600,?,?) ON CONFLICT(series,grain,bucket) DO UPDATE SET stats=excluded.stats",
                    (sid, bucket, dumps(combine(values, kinds[sid]))),
                )
            raw_floor, minute_floor, hour_floor = self.cutoffs()
            db.execute("DELETE FROM raw WHERE timestamp<?", (raw_floor,))
            db.execute(
                "DELETE FROM rollups WHERE grain=60 AND bucket<?", (minute_floor,)
            )
            db.execute(
                "DELETE FROM rollups WHERE grain=3600 AND bucket<?", (hour_floor,)
            )
            db.execute(
                "DELETE FROM batches WHERE received<?", (self.clock() - 14 * DAY,)
            )
            db.execute(
                "DELETE FROM rejections WHERE received<?", (self.clock() - 14 * DAY,)
            )
            empty = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM series s WHERE NOT EXISTS(SELECT 1 FROM raw r WHERE r.series=s.id) AND NOT EXISTS(SELECT 1 FROM rollups a WHERE a.series=s.id)"
                )
            ]
            for sid in empty:
                db.execute("DELETE FROM tags WHERE series=?", (sid,))
                db.execute("DELETE FROM series WHERE id=?", (sid,))
            db.execute(
                "DELETE FROM kinds WHERE NOT EXISTS(SELECT 1 FROM series s WHERE s.metric=kinds.metric)"
            )
            db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")
            db.execute(
                "UPDATE meta SET value=? WHERE key='compact_minute'", (current_minute,)
            )

    def select(self, db, metric, match):
        identifier(metric)
        match = labels(match)
        sql = "SELECT s.* FROM series s WHERE s.metric=?"
        args = [metric]
        for key, value in match.items():
            sql += " AND EXISTS(SELECT 1 FROM tags t WHERE t.series=s.id AND t.key=? AND t.value=?)"
            args.extend((key, value))
        rows = db.execute(sql + " ORDER BY s.id LIMIT 201", args).fetchall()
        if len(rows) > 200:
            raise Error(413, "query series budget exceeded; narrow labels")
        return rows

    def query(self, metric, start, end, match=None, step=None, aggregation="avg"):
        start, end = number(start), number(end)
        if (
            start > end
            or end > self.clock() + 30
            or start < self.clock() - 365 * DAY - 3600
        ):
            raise ValueError("invalid query range")
        self.compact()
        raw_floor, minute_floor, hour_floor = self.cutoffs()
        grain = 3600 if start < minute_floor else 60 if start < raw_floor else 1
        step = max(60, grain) if step is None else step
        if type(step) is not int or not grain <= step <= 86400 or step % grain:
            raise ValueError("query step must align with retained grain")
        match = labels(match or {})
        with self.transaction() as db:
            revision = self.meta(db)["revision"]
            key = dumps(
                [
                    revision,
                    metric,
                    match,
                    start,
                    end,
                    step,
                    aggregation,
                    raw_floor,
                    minute_floor,
                    hour_floor,
                ]
            )

            def load():
                output = []
                total_points = 0
                for series in self.select(db, metric, match):
                    atoms = []
                    rows = db.execute(
                        "SELECT timestamp,value FROM raw WHERE series=? AND timestamp>=? AND timestamp BETWEEN ? AND ? ORDER BY timestamp LIMIT 20001",
                        (series["id"], raw_floor, start, end),
                    ).fetchall()
                    if len(rows) > 20000:
                        raise Error(413, "raw query sample budget exceeded")
                    atoms.extend((row[0], 1, singleton(row[0], row[1])) for row in rows)
                    for size, lower, upper in [
                        (60, minute_floor, raw_floor),
                        (3600, hour_floor, minute_floor),
                    ]:
                        for row in db.execute(
                            "SELECT bucket,stats FROM rollups WHERE series=? AND grain=? AND bucket>=? AND bucket<? AND bucket<=? AND bucket+?>?",
                            (series["id"], size, lower, upper, end, size, start),
                        ):
                            atoms.append((row[0], size, json.loads(row[1])))
                    buckets = {}
                    approximate = False
                    for timestamp, size, stats in atoms:
                        bucket = math.floor(timestamp / step) * step
                        buckets.setdefault(bucket, []).append(stats)
                        if size > 1 and (timestamp < start or timestamp + size > end):
                            approximate = True
                    points = [
                        {
                            "timestamp": bucket,
                            "value": reading(
                                combine(values, series["kind"]),
                                aggregation,
                                series["kind"],
                            ),
                            "count": sum(value["count"] for value in values),
                        }
                        for bucket, values in sorted(buckets.items())
                    ]
                    total_points += len(points)
                    if total_points > 20000:
                        raise Error(413, "query output budget exceeded")
                    output.append(
                        {
                            "metric": metric,
                            "labels": json.loads(series["labels"]),
                            "type": series["kind"],
                            "points": points,
                            "partial_coarse_buckets": approximate,
                        }
                    )
                return {
                    "revision": revision,
                    "step": step,
                    "minimum_grain": grain,
                    "series": output,
                }

            return self.cache.get(key, load)

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

    def archive(self, path, start, end):
        start, end = number(start), number(end)
        if start >= end or end > self.clock() - 30 * DAY:
            raise ValueError("archive targets completed cold ranges older than 30 days")
        self.compact()
        with self.transaction() as db:
            buckets = [
                {
                    "series": r["series"],
                    "bucket": r["bucket"],
                    "stats": json.loads(r["stats"]),
                }
                for r in db.execute(
                    "SELECT * FROM rollups WHERE grain=3600 AND bucket>=? AND bucket<? ORDER BY series,bucket",
                    (start, end),
                )
            ]
            ids = {bucket["series"] for bucket in buckets}
            series = [
                dict(r)
                for r in db.execute("SELECT * FROM series ORDER BY id")
                if r["id"] in ids
            ]
        payload = {
            "format": "metrics-hour-archive-v1",
            "start": start,
            "end": end,
            "series": series,
            "buckets": buckets,
        }
        encoded = gzip.compress(
            dumps({"payload": payload, "sha256": digest(payload)}).encode()
        )
        with open(path, "xb") as output:
            output.write(encoded)
        return {
            "buckets": len(buckets),
            "gzip_bytes": len(encoded),
            "tier": "local archive, not cloud storage",
        }


def read_archive(path):
    with gzip.open(path, "rb") as source:
        encoded = source.read(64 * 1024 * 1024 + 1)
    if len(encoded) > 64 * 1024 * 1024:
        raise ValueError("archive exceeds 64 MiB reference limit")
    envelope = json.loads(encoded)
    if (
        digest(envelope["payload"]) != envelope["sha256"]
        or envelope["payload"]["format"] != "metrics-hour-archive-v1"
    ):
        raise ValueError("archive checksum or format invalid")
    return envelope["payload"]
