from contextlib import closing, contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

from .cache import Cache as Cache


class Error(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value):
        raise ValueError("invalid identifier")
    return value


def labels(value):
    if not isinstance(value, dict) or len(value) > 8:
        raise ValueError("at most eight labels required")
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,31}", key)
            or not isinstance(item, str)
            or len(item) > 128
        ):
            raise ValueError("invalid label")
    return dict(sorted(value.items()))


def number(value):
    if (
        type(value) not in (int, float)
        or not -1e15 <= value <= 1e15
        or not math.isfinite(value)
    ):
        raise ValueError("finite bounded numeric value required")
    return float(value)


def sample(value):
    if not isinstance(value, dict) or set(value) - {
        "metric",
        "labels",
        "type",
        "timestamp",
        "value",
    }:
        raise ValueError("invalid sample object")
    metric = identifier(value["metric"])
    kind = value.get("type", "gauge")
    if kind not in ("gauge", "counter"):
        raise ValueError("type must be gauge or counter")
    reading = number(value["value"])
    if kind == "counter" and reading < 0:
        raise ValueError("counter must be nonnegative")
    return {
        "metric": metric,
        "labels": labels(value.get("labels", {})),
        "type": kind,
        "timestamp": number(value["timestamp"]),
        "value": reading,
    }


class Database:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            return db
        except BaseException:
            db.close()
            raise

    @contextmanager
    def transaction(self, write=False):
        with closing(self.connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise


def http_json(
    url,
    method="GET",
    body=None,
    token=None,
    headers=None,
    timeout=5,
    *,
    deadline=None,
    cancel=None,
    max_bytes=4 * 1024 * 1024,
):
    """Compatibility entry point for the direct, bounded HTTP transport."""
    from .transport import request

    return request(
        url,
        method,
        body,
        token,
        headers,
        timeout,
        deadline=deadline,
        cancel=cancel,
        max_bytes=max_bytes,
    )
