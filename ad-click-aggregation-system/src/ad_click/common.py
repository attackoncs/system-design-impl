from contextlib import contextmanager
import hashlib
import ipaddress
import json
from pathlib import Path
import sqlite3
from urllib.request import Request, urlopen


class Error(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def text(value, name):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} must contain 1..128 printable characters")
    return value


def integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low},{high}]")
    return value


def event(body, now):
    fields = {"event_id", "ad_id", "click_timestamp", "user_id", "ip", "country"}
    if not isinstance(body, dict) or set(body) != fields:
        raise ValueError("event requires exactly " + ", ".join(sorted(fields)))
    result = {k: text(body[k], k) for k in fields - {"click_timestamp"}}
    result["click_timestamp"] = integer(body["click_timestamp"], "click_timestamp", 0, int(now) + 30)
    result["ip"] = str(ipaddress.ip_address(result["ip"]))
    if len(result["country"]) != 2 or not result["country"].isascii() or not result["country"].isalpha():
        raise ValueError("country must be a two-letter code")
    result["country"] = result["country"].upper()
    return result


def query(body, now):
    if not isinstance(body, dict) or set(body) - {"from", "to", "window", "count", "ad_id", "filters"}:
        raise ValueError("unknown query fields")
    end = body.get("to", int(now) // 60 * 60)
    window = integer(body.get("window", 1), "window", 1, 1440)
    start = body.get("from", end - window * 60)
    integer(start, "from", 0, 2**53)
    integer(end, "to", 0, 2**53)
    if start % 60 or end % 60 or not 0 < end - start <= 86400:
        raise ValueError("range must be minute-aligned [from,to), at most 1440 minutes")
    count = integer(body.get("count", 100), "count", 1, 1000)
    filters = body.get("filters", {})
    if not isinstance(filters, dict) or set(filters) - {"user_id", "ip", "country"}:
        raise ValueError("filters support user_id, ip and country only")
    filters = {k: text(v, k) for k, v in filters.items()}
    if "ip" in filters:
        filters["ip"] = str(ipaddress.ip_address(filters["ip"]))
    if "country" in filters:
        filters["country"] = filters["country"].upper()
    result = {"from": start, "to": end, "count": count, "filters": filters}
    if "ad_id" in body:
        result["ad_id"] = text(body["ad_id"], "ad_id")
    return result


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

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

    def initialize(self, sql):
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(sql)
            db.commit()
        finally:
            db.close()


def http_json(url, token, body=None, timeout=5):
    data = None if body is None else dumps(body).encode()
    request = Request(url, data=data, headers={"Authorization": "Bearer " + token,
                                             "Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise Error(413, "response exceeded 8 MiB")
        return json.loads(raw)
