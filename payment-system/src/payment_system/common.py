from contextlib import contextmanager
import hashlib
import hmac
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


def http_json(
    url,
    token=None,
    body=None,
    method=None,
    headers=None,
    *,
    timeout=5,
    deadline=None,
    cancel=None,
):
    from .transport import request

    return request(
        url,
        method or ("GET" if body is None else "POST"),
        body=body,
        token=token,
        headers=headers,
        timeout=timeout,
        deadline=deadline,
        cancel=cancel,
    )
