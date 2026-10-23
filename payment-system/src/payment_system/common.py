from contextlib import contextmanager
import hashlib
import hmac
from io import BytesIO
import json
from pathlib import Path
import re
import sqlite3
from urllib.error import HTTPError
from urllib.request import Request, urlopen


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


def money(value):
    return integer(value, "amount (integer USD cents)", 1, 100_000_000)


def signature(secret, timestamp, raw):
    return hmac.new(
        secret.encode(), str(timestamp).encode() + b"." + raw, hashlib.sha256
    ).hexdigest()


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


def http_json(url, token=None, body=None, method=None, headers=None):
    req = Request(
        url,
        data=None if body is None else dumps(body).encode(),
        method=method,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": "Bearer " + token} if token else {}),
            **(headers or {}),
        },
    )
    try:
        response = urlopen(req, timeout=5)
    except HTTPError as exc:
        try:
            data = exc.read(1024 * 1024)
        finally:
            exc.close()
        raise HTTPError(
            exc.url, exc.code, exc.msg, exc.headers, BytesIO(data)
        ) from None
    with response:
        data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise Error(413, "response exceeds reference budget")
        return json.loads(data)
