from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3

CHUNK = 256 * 1024


class Error(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(value):
    return sha(dumps(value).encode())


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("invalid identifier")
    return value


def checksum(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("expected lowercase SHA256")
    return value


def object_key(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 512
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("object key requires 1..512 UTF-8 bytes without controls")
    return value


def integer(value, name, low=0, high=2**53):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError("invalid " + name)
    return value


def shard_for(bucket, key, count):
    return int(digest([bucket, object_key(key)])[:16], 16) % count


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


def request(
    url,
    token,
    body=None,
    method=None,
    raw=False,
    headers=None,
    *,
    timeout=5,
    deadline=None,
    cancel=None,
):
    from .transport import request as direct_request

    return direct_request(
        url,
        method or ("POST" if body is not None else "GET"),
        body,
        token,
        headers,
        timeout,
        deadline=deadline,
        cancel=cancel,
        raw=raw,
    )
