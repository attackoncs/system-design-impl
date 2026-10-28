from .cache import Cache as Cache
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3


class Error(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError(
            "IDs must contain 1..80 ASCII letters, digits, underscores or hyphens"
        )
    return value


def integer(value, name, low, high):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be an integer in [{low},{high}]")
    return value


def text(value, name, maximum=1000):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError(f"invalid {name}")
    return value


def stay(start, end, now, future=True):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if (
        first.isoformat() != start
        or last.isoformat() != end
        or not 1 <= (last - first).days <= 30
    ):
        raise ValueError(
            "stay must contain 1..30 nights with canonical YYYY-MM-DD dates"
        )
    today = datetime.fromtimestamp(now, timezone.utc).date()
    if future and (first < today or last > today + timedelta(days=730)):
        raise ValueError("stay must be within the next 730 UTC calendar days")
    return [(first + timedelta(days=i)).isoformat() for i in range((last - first).days)]


def shard_for(hotel_id, count):
    integer(count, "shard count", 1, 32)
    return int(digest(identifier(hotel_id))[:16], 16) % count


def booking_hotel(booking_id):
    if not isinstance(booking_id, str):
        raise ValueError("invalid reservation ID")
    pieces = booking_id.split(".")
    if len(pieces) != 2 or not re.fullmatch(r"[a-f0-9]{32}", pieces[1]):
        raise ValueError("invalid reservation ID")
    return identifier(pieces[0])


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def initialize(self, sql):
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(sql)
            db.commit()
        finally:
            db.close()

    @contextmanager
    def transaction(self, write=False):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


def http_json(
    url,
    token,
    body=None,
    method=None,
    timeout=5,
    *,
    deadline=None,
    cancel=None,
    max_bytes=4 * 1024 * 1024,
):
    from .transport import request

    return request(
        url,
        method or ("POST" if body is not None else "GET"),
        body,
        token,
        timeout=timeout,
        deadline=deadline,
        cancel=cancel,
        max_bytes=max_bytes,
    )
