from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import sqlite3
from urllib.request import Request, urlopen
from urllib.error import HTTPError


class Error(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("invalid identifier")
    return value


def integer(value, name, low=0, high=2**53):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError("invalid " + name)
    return value


def month_bounds(month):
    if not isinstance(month, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
        raise ValueError("month must be YYYY-MM")
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    end = datetime(
        start.year + int(start.month == 12),
        start.month % 12 + 1,
        1,
        tzinfo=timezone.utc,
    )
    return int(start.timestamp()), int(end.timestamp())


def current_month(now):
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")


def shard_for(user, count):
    return int(digest(identifier(user))[:16], 16) % count


def lex_member(user, score):
    return f"{1_000_000_000 - score:010d}:" + user


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
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


def http_json(url, token, body=None, method=None):
    request = Request(
        url,
        data=None if body is None else dumps(body).encode(),
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as exc:
        try:
            payload = exc.read(1024 * 1024)
        finally:
            exc.close()
        # Preserve status/body for callers without leaving a failed socket open
        # during projection readiness retries.
        raise HTTPError(
            exc.url, exc.code, exc.msg, exc.headers, BytesIO(payload)
        ) from None
    with response:
        data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise Error(413, "response budget exceeded")
        return json.loads(data)
