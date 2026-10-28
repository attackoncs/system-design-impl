"""Incremental rollups computed from a consistent read snapshot, outside writer.

Publication retries if ingestion changed the storage revision. Work is bounded;
larger installations need incremental jobs or an external time-series engine.
"""

import json
import math

from .common import Error, dumps
from .summaries import combine, singleton

DAY = 86400
MAX_ROLLUP_BUCKETS = 20000
MAX_ROLLUP_ATOMS = 200000


class Compaction:
    def _rollup_snapshot(self):
        now = self._now()
        current_minute = math.floor(now / 60)
        watermark = math.floor((now - 120) / 60) * 60
        closed_hour = math.floor((now - 120) / 3600) * 3600
        with self.transaction() as db:
            meta = self.meta(db)
            if meta["compact_minute"] == current_minute:
                return None
            minute_keys = [
                tuple(row)
                for row in db.execute(
                    "SELECT series,bucket FROM dirty_minutes WHERE bucket<? ORDER BY bucket,series LIMIT ?",
                    (watermark, MAX_ROLLUP_BUCKETS + 1),
                )
            ]
            if len(minute_keys) > MAX_ROLLUP_BUCKETS:
                raise Error(503, "rollup bucket budget exceeded")
            hour_keys = {
                tuple(row)
                for row in db.execute(
                    "SELECT series,bucket FROM dirty_hours WHERE bucket<? ORDER BY bucket,series LIMIT ?",
                    (closed_hour, MAX_ROLLUP_BUCKETS + 1),
                )
            }
            hour_keys.update(
                (sid, bucket // 3600 * 3600)
                for sid, bucket in minute_keys
                if bucket < closed_hour
            )
            if len(hour_keys) > MAX_ROLLUP_BUCKETS:
                raise Error(503, "rollup bucket budget exceeded")
            kinds = {}
            raw = {}
            hours = {}
            total = 0

            def kind(sid):
                if sid not in kinds:
                    row = db.execute(
                        "SELECT kind FROM series WHERE id=?", (sid,)
                    ).fetchone()
                    if row is None:
                        raise Error(503, "rollup series metadata missing")
                    kinds[sid] = row[0]

            for sid, bucket in minute_keys:
                kind(sid)
                rows = [
                    tuple(row)
                    for row in db.execute(
                        "SELECT timestamp,value FROM raw WHERE series=? AND timestamp>=? AND timestamp<? ORDER BY timestamp LIMIT ?",
                        (sid, bucket, bucket + 60, MAX_ROLLUP_ATOMS - total + 1),
                    )
                ]
                total += len(rows)
                if total > MAX_ROLLUP_ATOMS:
                    raise Error(503, "rollup sample budget exceeded")
                raw[sid, bucket] = rows
            for sid, bucket in sorted(hour_keys):
                kind(sid)
                rows = [
                    (row[0], row[1])
                    for row in db.execute(
                        "SELECT bucket,stats FROM rollups WHERE series=? AND grain=60 AND bucket>=? AND bucket<? ORDER BY bucket LIMIT ?",
                        (sid, bucket, bucket + 3600, MAX_ROLLUP_ATOMS - total + 1),
                    )
                ]
                total += len(rows)
                if total > MAX_ROLLUP_ATOMS:
                    raise Error(503, "rollup sample budget exceeded")
                hours[sid, bucket] = rows
        # No SQLite writer is held while sorting/combining/encoding summaries.
        minute_plan = {}
        for key, rows in raw.items():
            stats = combine(
                [singleton(timestamp, value) for timestamp, value in rows],
                kinds[key[0]],
            )
            if stats:
                minute_plan[key] = stats
        hour_plan = {}
        for (sid, bucket), rows in hours.items():
            merged = {minute: json.loads(stats) for minute, stats in rows}
            for minute in range(bucket, bucket + 3600, 60):
                if (sid, minute) in minute_plan:
                    merged[minute] = minute_plan[sid, minute]
            stats = combine(list(merged.values()), kinds[sid])
            if stats:
                hour_plan[sid, bucket] = stats
        encoded_minutes = [
            (sid, bucket, dumps(stats)) for (sid, bucket), stats in minute_plan.items()
        ]
        encoded_hours = [
            (sid, bucket, dumps(stats)) for (sid, bucket), stats in hour_plan.items()
        ]
        return (
            meta["revision"],
            now,
            current_minute,
            minute_keys,
            hour_keys,
            encoded_minutes,
            encoded_hours,
        )

    def compact(self):
        # Per-instance lock coalesces concurrent query maintenance. Other Store
        # instances/processes use the revision check, not this in-process lock.
        with self._compaction:
            for _ in range(3):
                plan = self._rollup_snapshot()
                if plan is None:
                    return
                revision, now, minute, minute_keys, hour_keys, minutes, hours = plan
                with self.transaction(True) as db:
                    if self.meta(db)["revision"] != revision:
                        continue
                    db.executemany(
                        "INSERT INTO rollups VALUES(?,60,?,?) ON CONFLICT(series,grain,bucket) DO UPDATE SET stats=excluded.stats",
                        minutes,
                    )
                    db.executemany(
                        "INSERT OR IGNORE INTO dirty_hours VALUES(?,?)",
                        [(sid, bucket // 3600 * 3600) for sid, bucket in minute_keys],
                    )
                    db.executemany(
                        "DELETE FROM dirty_minutes WHERE series=? AND bucket=?",
                        minute_keys,
                    )
                    db.executemany(
                        "INSERT INTO rollups VALUES(?,3600,?,?) ON CONFLICT(series,grain,bucket) DO UPDATE SET stats=excluded.stats",
                        hours,
                    )
                    db.executemany(
                        "DELETE FROM dirty_hours WHERE series=? AND bucket=?",
                        list(hour_keys),
                    )
                    raw_floor, minute_floor, hour_floor = self.cutoffs(now)
                    db.execute("DELETE FROM raw WHERE timestamp<?", (raw_floor,))
                    db.execute(
                        "DELETE FROM rollups WHERE grain=60 AND bucket<?",
                        (minute_floor,),
                    )
                    db.execute(
                        "DELETE FROM rollups WHERE grain=3600 AND bucket<?",
                        (hour_floor,),
                    )
                    db.execute(
                        "DELETE FROM batches WHERE received<?", (now - 14 * DAY,)
                    )
                    db.execute(
                        "DELETE FROM rejections WHERE received<?", (now - 14 * DAY,)
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
                        db.execute("DELETE FROM dirty_minutes WHERE series=?", (sid,))
                        db.execute("DELETE FROM dirty_hours WHERE series=?", (sid,))
                    db.execute(
                        "DELETE FROM kinds WHERE NOT EXISTS(SELECT 1 FROM series s WHERE s.metric=kinds.metric)"
                    )
                    db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")
                    db.execute(
                        "UPDATE meta SET value=? WHERE key='compact_minute'", (minute,)
                    )
                    return
            raise Error(503, "rollup snapshot changed; retry maintenance")
