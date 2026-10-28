"""Bounded snapshot queries and explicit cross-series gauge aggregation."""

import json
import math

from .common import Error, dumps, identifier, labels, number
from .summaries import combine, reading, singleton

DAY = 86400
MAX_QUERY_ATOMS = 200000


def grouping(value):
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) > 8
        or any(not isinstance(item, str) for item in value)
    ):
        raise ValueError("group_by requires up to eight label names")
    if len(set(value)) != len(value):
        raise ValueError("group_by names must be unique")
    labels({key: "" for key in value})
    return sorted(value)


class Queries:
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

    def query(
        self,
        metric,
        start,
        end,
        match=None,
        step=None,
        aggregation="avg",
        group_by=None,
    ):
        metric = identifier(metric)
        start, end = number(start), number(end)
        match = labels({} if match is None else match)
        group_by = grouping(group_by)
        if aggregation not in ("avg", "min", "max", "last", "increase", "rate"):
            raise ValueError("invalid aggregation")
        if group_by is not None and aggregation not in ("avg", "min", "max"):
            raise ValueError("cross-series grouping supports gauge avg/min/max")
        # Ingestion invalidates compact_minute atomically. Read that marker and
        # revision in the same snapshot used by the loader: a late sample cannot
        # sneak between compaction and a read of its old rollup.
        for _ in range(3):
            now = self._now()
            if start > end or end > now + 30 or start < now - 365 * DAY - 3600:
                raise ValueError("invalid query range")
            self.compact()
            now = self._now()
            raw_floor, minute_floor, hour_floor = self.cutoffs(now)
            grain = 3600 if start < minute_floor else 60 if start < raw_floor else 1
            actual_step = max(60, grain) if step is None else step
            if (
                type(actual_step) is not int
                or not grain <= actual_step <= 86400
                or actual_step % grain
            ):
                raise ValueError("query step must align with retained grain")
            with self.transaction() as db:
                meta = self.meta(db)
                if meta["compact_minute"] != math.floor(now / 60):
                    continue
                revision = meta["revision"]
                key = dumps(
                    [
                        revision,
                        metric,
                        match,
                        start,
                        end,
                        actual_step,
                        aggregation,
                        group_by,
                        raw_floor,
                        minute_floor,
                        hour_floor,
                    ]
                )
                return self.cache.get(
                    key,
                    lambda: self._query_snapshot(
                        db,
                        metric,
                        start,
                        end,
                        match,
                        actual_step,
                        aggregation,
                        group_by,
                        revision,
                        grain,
                        (raw_floor, minute_floor, hour_floor),
                    ),
                )
        raise Error(503, "query snapshot changed; retry")

    def _query_snapshot(
        self,
        db,
        metric,
        start,
        end,
        match,
        step,
        aggregation,
        group_by,
        revision,
        grain,
        cutoffs,
    ):
        raw_floor, minute_floor, hour_floor = cutoffs
        output = []
        groups = {}
        total_atoms = total_points = 0
        for series in self.select(db, metric, match):
            if group_by is not None and series["kind"] != "gauge":
                raise ValueError("cross-series grouping supports gauge metrics only")
            # Even when there are no points, reject a kind/aggregation mismatch.
            reading(singleton(0, 0), aggregation, series["kind"])
            atoms = []
            remaining = min(20000, MAX_QUERY_ATOMS - total_atoms)
            rows = db.execute(
                "SELECT timestamp,value FROM raw WHERE series=? AND timestamp>=? AND timestamp BETWEEN ? AND ? ORDER BY timestamp LIMIT ?",
                (series["id"], raw_floor, start, end, remaining + 1),
            ).fetchall()
            if len(rows) > remaining:
                raise Error(413, "raw query sample budget exceeded")
            total_atoms += len(rows)
            atoms.extend((row[0], 1, singleton(row[0], row[1])) for row in rows)
            for size, lower, upper in [
                (60, minute_floor, raw_floor),
                (3600, hour_floor, minute_floor),
            ]:
                rows = db.execute(
                    "SELECT bucket,stats FROM rollups WHERE series=? AND grain=? AND bucket>=? AND bucket<? AND bucket<=? AND bucket+?>? ORDER BY bucket LIMIT ?",
                    (
                        series["id"],
                        size,
                        lower,
                        upper,
                        end,
                        size,
                        start,
                        MAX_QUERY_ATOMS - total_atoms + 1,
                    ),
                ).fetchall()
                total_atoms += len(rows)
                if total_atoms > MAX_QUERY_ATOMS:
                    raise Error(413, "query input budget exceeded")
                atoms.extend((row[0], size, json.loads(row[1])) for row in rows)
            buckets = {}
            approximate = False
            for timestamp, size, stats in atoms:
                bucket = math.floor(timestamp / step) * step
                buckets.setdefault(bucket, []).append(stats)
                if size > 1 and (timestamp < start or timestamp + size > end):
                    approximate = True
            labelset = json.loads(series["labels"])
            if group_by is None:
                total_points += len(buckets)
                if total_points > 20000:
                    raise Error(413, "query output budget exceeded")
                output.append(
                    {
                        "metric": metric,
                        "labels": labelset,
                        "type": series["kind"],
                        "points": self._points(buckets, aggregation, series["kind"]),
                        "partial_coarse_buckets": approximate,
                    }
                )
            else:
                selected_labels = {
                    key: labelset[key] for key in group_by if key in labelset
                }
                identity = dumps(selected_labels)
                group = groups.setdefault(
                    identity,
                    {
                        "metric": metric,
                        "labels": selected_labels,
                        "type": "gauge",
                        "buckets": {},
                        "partial_coarse_buckets": False,
                        "source_series": 0,
                    },
                )
                group["source_series"] += 1
                group["partial_coarse_buckets"] |= approximate
                for bucket, values in buckets.items():
                    if bucket not in group["buckets"]:
                        total_points += 1
                        if total_points > 20000:
                            raise Error(413, "query output budget exceeded")
                    group["buckets"].setdefault(bucket, []).append(
                        combine(values, "gauge")
                    )
        if group_by is not None:
            for _, group in sorted(groups.items()):
                group["points"] = self._points(
                    group.pop("buckets"), aggregation, "gauge"
                )
                output.append(group)
        result = {
            "revision": revision,
            "step": step,
            "minimum_grain": grain,
            "series": output,
        }
        if group_by is not None:
            result["group_by"] = group_by
        return result

    @staticmethod
    def _points(buckets, aggregation, kind):
        return [
            {
                "timestamp": bucket,
                "value": reading(combine(values, kind), aggregation, kind),
                "count": sum(value["count"] for value in values),
            }
            for bucket, values in sorted(buckets.items())
        ]
