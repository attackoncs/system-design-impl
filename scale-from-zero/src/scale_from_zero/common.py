from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.error import HTTPError


class Error(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def checksum(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError(
            "identifier must be 1..80 ASCII letters, digits, underscores or hyphens"
        )
    return value


def integer(value, minimum=0, maximum=10**12):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("integer cents/sequence outside reference limits")
    return value


def shard_for(account, count):
    return int(hashlib.sha256(identifier(account).encode()).hexdigest(), 16) % count


def request(url, token, body=None, timeout=10, *, deadline=None, cancel=None):
    from .transport import request as rpc

    try:
        return rpc(
            url,
            "GET" if body is None else "POST",
            body,
            token or None,
            timeout=timeout,
            deadline=deadline,
            cancel=cancel,
        )
    except HTTPError as exc:
        try:
            raw = exc.read(65536)
        finally:
            exc.close()
        raise Error(exc.code, raw.decode(errors="replace")) from None


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def initialize(self, sql):
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(sql)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    @contextmanager
    def read_transaction(self):
        """Pin related reads to one SQLite snapshot, including WAL readers."""
        with self.connection() as db:
            db.execute("BEGIN")
            try:
                yield db
            finally:
                db.rollback()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise


def entry(seq, previous, event):
    value = {"seq": seq, "previous": previous, "event": event}
    return {**value, "hash": checksum(value)}


def verify(rows):
    previous = "0" * 64
    for index, row in enumerate(rows, 1):
        if row != entry(index, previous, row["event"]):
            raise ValueError("event log checksum or sequence mismatch")
        previous = row["hash"]
    return previous
