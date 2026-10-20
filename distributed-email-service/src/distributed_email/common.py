from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
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
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value):
        raise ValueError("invalid identifier")
    return value


def integer(value, name, low=0, high=2**53):
    if isinstance(value, bool) or not isinstance(value, int) or not low<=value<=high:
        raise ValueError(f"invalid {name}")
    return value


def text(value, name, maximum):
    if not isinstance(value, str) or len(value)>maximum or "\x00" in value:
        raise ValueError(f"invalid {name}")
    return value


def shard_for(user, shards):
    return int(digest(identifier(user))[:16], 16)%shards


def terms(value):
    return sorted(set(re.findall(r"\w+", value.casefold(), re.UNICODE)))


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
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


def http_json(url, token, body=None, method=None, timeout=5):
    request = Request(url, data=None if body is None else dumps(body).encode(), method=method,
                      headers={"Authorization": "Bearer "+token, "Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(20*1024*1024+1)
        if len(raw)>20*1024*1024:
            raise Error(413, "response exceeds 20 MiB")
        return json.loads(raw)
