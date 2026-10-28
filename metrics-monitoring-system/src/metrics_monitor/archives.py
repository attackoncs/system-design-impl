"""Bounded, immutable local hourly archives with checksum and schema checks."""

import gzip
import json
import math
import os
from pathlib import Path

from .common import Error, digest, dumps, identifier, labels, number

DAY = 86400
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BUCKETS = 100000
MAX_ARCHIVE_SERIES = 10000


def _range(start, end):
    start, end = number(start), number(end)
    if start >= end or start % 3600 or end % 3600:
        raise ValueError("archive range requires increasing whole hours")
    return start, end


class Archives:
    def archive(self, path, start, end):
        start, end = _range(start, end)
        if end > self._now() - 30 * DAY:
            raise ValueError("archive targets completed cold ranges older than 30 days")
        self.compact()
        with self.transaction() as db:
            rows = db.execute(
                "SELECT series,bucket,stats FROM rollups WHERE grain=3600 AND bucket>=? AND bucket<? ORDER BY series,bucket LIMIT ?",
                (start, end, MAX_ARCHIVE_BUCKETS + 1),
            ).fetchall()
            if len(rows) > MAX_ARCHIVE_BUCKETS:
                raise Error(413, "archive bucket budget exceeded")
            series = [
                dict(row)
                for row in db.execute(
                    "SELECT s.* FROM series s WHERE EXISTS(SELECT 1 FROM rollups r WHERE r.series=s.id AND r.grain=3600 AND r.bucket>=? AND r.bucket<?) ORDER BY s.id LIMIT ?",
                    (start, end, MAX_ARCHIVE_SERIES + 1),
                )
            ]
            if len(series) > MAX_ARCHIVE_SERIES:
                raise Error(413, "archive series budget exceeded")
        buckets = [
            {"series": row[0], "bucket": row[1], "stats": json.loads(row[2])}
            for row in rows
        ]
        payload = {
            "format": "metrics-hour-archive-v1",
            "start": start,
            "end": end,
            "series": series,
            "buckets": buckets,
        }
        raw = dumps({"payload": payload, "sha256": digest(payload)}).encode()
        if len(raw) > MAX_ARCHIVE_BYTES:
            raise Error(413, "archive exceeds 64 MiB decoded limit")
        encoded = gzip.compress(raw)
        if len(encoded) > MAX_ARCHIVE_BYTES:
            raise Error(413, "archive exceeds 64 MiB compressed limit")
        destination = Path(path)
        output = destination.open("xb")
        created = os.fstat(output.fileno())
        try:
            with output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            try:
                existing = destination.stat()
                if (existing.st_dev, existing.st_ino) == (
                    created.st_dev,
                    created.st_ino,
                ):
                    destination.unlink()
            except FileNotFoundError:
                pass
            raise
        return {
            "buckets": len(buckets),
            "gzip_bytes": len(encoded),
            "tier": "local archive, not cloud storage",
        }


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate archive field")
        result[key] = value
    return result


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _statistics(stats, bucket, kind):
    fields = {
        "count",
        "sum",
        "min",
        "max",
        "first_t",
        "first",
        "last_t",
        "last",
        "increase",
    }
    if (
        not isinstance(stats, dict)
        or set(stats) != fields
        or type(stats["count"]) is not int
        or not 1 <= stats["count"] <= 2**53 - 1
    ):
        raise ValueError("invalid archive statistics")
    if any(not _finite(value) for field, value in stats.items() if field != "count"):
        raise ValueError("invalid archive statistics")
    if not bucket <= stats["first_t"] <= stats["last_t"] < bucket + 3600:
        raise ValueError("archive statistics outside bucket")
    if (
        not stats["min"] <= stats["first"] <= stats["max"]
        or not stats["min"] <= stats["last"] <= stats["max"]
        or stats["increase"] < 0
    ):
        raise ValueError("invalid archive extrema")
    if kind == "counter" and stats["min"] < 0:
        raise ValueError("negative archived counter")


def read_archive(path):
    with Path(path).open("rb") as source:
        if os.fstat(source.fileno()).st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("archive exceeds 64 MiB compressed limit")
        try:
            with gzip.GzipFile(fileobj=source, mode="rb") as decoded:
                encoded = decoded.read(MAX_ARCHIVE_BYTES + 1)
        except (OSError, EOFError) as exc:
            raise ValueError("invalid gzip archive") from exc
    if len(encoded) > MAX_ARCHIVE_BYTES:
        raise ValueError("archive exceeds 64 MiB decoded limit")
    try:
        envelope = json.loads(encoded, object_pairs_hook=_pairs)
        if not isinstance(envelope, dict) or set(envelope) != {"payload", "sha256"}:
            raise ValueError("invalid archive envelope")
        payload = envelope["payload"]
        if (
            not isinstance(payload, dict)
            or set(payload) != {"format", "start", "end", "series", "buckets"}
            or payload["format"] != "metrics-hour-archive-v1"
            or digest(payload) != envelope["sha256"]
        ):
            raise ValueError("archive checksum or format invalid")
        start, end = _range(payload["start"], payload["end"])
        if (
            not isinstance(payload["series"], list)
            or len(payload["series"]) > MAX_ARCHIVE_SERIES
            or not isinstance(payload["buckets"], list)
            or len(payload["buckets"]) > MAX_ARCHIVE_BUCKETS
        ):
            raise ValueError("archive object budget exceeded")
        series = {}
        kinds = {}
        for row in payload["series"]:
            if (
                not isinstance(row, dict)
                or set(row) != {"id", "metric", "labels", "kind"}
                or row["kind"] not in ("gauge", "counter")
            ):
                raise ValueError("invalid archive series")
            metric = identifier(row["metric"])
            labelset = labels(json.loads(row["labels"], object_pairs_hook=_pairs))
            if (
                row["id"] != digest([metric, labelset])
                or row["id"] in series
                or metric in kinds
                and kinds[metric] != row["kind"]
            ):
                raise ValueError("archive series identity conflict")
            kinds[metric] = row["kind"]
            series[row["id"]] = row
        seen = set()
        for row in payload["buckets"]:
            if (
                not isinstance(row, dict)
                or set(row) != {"series", "bucket", "stats"}
                or not isinstance(row["series"], str)
                or row["series"] not in series
            ):
                raise ValueError("invalid archive bucket")
            bucket = row["bucket"]
            if (
                type(bucket) is not int
                or bucket % 3600
                or not start <= bucket < end
                or (row["series"], bucket) in seen
            ):
                raise ValueError("archive bucket range or identity invalid")
            seen.add((row["series"], bucket))
            _statistics(row["stats"], bucket, series[row["series"]]["kind"])
        return payload
    except (KeyError, TypeError, OverflowError, RecursionError) as exc:
        raise ValueError("invalid archive structure") from exc
