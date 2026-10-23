import base64
from collections import OrderedDict
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid

from .geo import coordinates, cover, distance, encode


class Error(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Cache:
    def __init__(self, ttl=60, capacity=2048, clock=time.monotonic):
        self.ttl, self.capacity, self.clock = ttl, capacity, clock
        self.items = OrderedDict()
        self.hits = self.misses = 0
        self.lock = threading.Lock()

    def get(self, key, loader):
        with self.lock:
            now = self.clock()
            if key in self.items and self.items[key][0] > now:
                self.hits += 1
                self.items.move_to_end(key)
                return self.items[key][1]
            self.misses += 1
            value = loader()
            self.items[key] = (now + self.ttl, value)
            self.items.move_to_end(key)
            while len(self.items) > self.capacity:
                self.items.popitem(last=False)
            return value


def validate_business(body):
    if not isinstance(body, dict):
        raise ValueError("business must be an object")
    allowed = {"name", "latitude", "longitude", "address", "category", "hours"}
    if set(body) - allowed:
        raise ValueError("unknown business fields")
    result = dict(body)
    for key in ("name", "address", "category"):
        default = "" if key == "address" else "other" if key == "category" else None
        value = result.get(key, default)
        if (
            not isinstance(value, str)
            or (key != "address" and not value.strip())
            or len(value) > 500
        ):
            raise ValueError(f"invalid {key}")
        result[key] = value
    result["latitude"], result["longitude"] = coordinates(
        body.get("latitude"), body.get("longitude")
    )
    hours = result.setdefault("hours", [])
    if not isinstance(hours, list) or len(hours) > 100:
        raise ValueError("invalid hours")
    for window in hours:
        if (
            not isinstance(window, dict)
            or set(window) != {"day", "start", "end"}
            or any(type(window[k]) is not int for k in window)
            or not 0 <= window["day"] <= 6
            or not 0 <= window["start"] < window["end"] <= 1440
        ):
            raise ValueError("hours require UTC day 0..6 and 0 <= start < end <= 1440")
    return result


class Store:
    def __init__(self, path, shards=4, clock=time.time, readonly=False):
        if not 1 <= shards <= 256:
            raise ValueError("shards must be 1..256")
        self.path, self.shards, self.clock, self.readonly = (
            str(path),
            shards,
            clock,
            readonly,
        )
        self.candidates, self.details = Cache(), Cache()
        with closing(self.connect()) as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY,owner TEXT NOT NULL,body TEXT NOT NULL,deleted INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS public(id TEXT PRIMARY KEY,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS geo(precision INTEGER,cell TEXT,shard INTEGER,id TEXT,PRIMARY KEY(precision,cell,shard,id));
            CREATE TABLE IF NOT EXISTS replay(owner TEXT,key TEXT,fingerprint TEXT,response TEXT,created REAL,PRIMARY KEY(owner,key));
            """)
            for key, value in {
                "version": "0",
                "day": self.day(),
                "lineage": str(uuid.uuid4()),
                "replica_initialized": "0",
                "shards": str(shards),
            }.items():
                db.execute("INSERT OR IGNORE INTO meta VALUES(?,?)", (key, value))
            self.shards = int(self.metadata(db)["shards"])
            db.commit()

    def day(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute("PRAGMA busy_timeout=15000")
        return db

    @contextmanager
    def transaction(self, write=False):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def metadata(self, db):
        return dict(db.execute("SELECT key,value FROM meta"))

    def mutate(self, owner, method, identifier, body, key):
        if self.readonly:
            raise Error(403, "replica is read only")
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise Error(400, "Idempotency-Key required (1..128 characters)")
        if identifier is not None and not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", identifier
        ):
            raise Error(400, "invalid business ID")
        if method != "DELETE":
            body = validate_business(body)
        fingerprint = hashlib.sha256(
            canonical([method, identifier, body]).encode()
        ).hexdigest()
        with self.transaction(True) as db:
            # Freeze yesterday's drafts before accepting today's first mutation.
            self._publication(db)
            db.execute(
                "DELETE FROM replay WHERE created < ?", (self.clock() - 7 * 86400,)
            )
            replay = db.execute(
                "SELECT fingerprint,response FROM replay WHERE owner=? AND key=?",
                (owner, key),
            ).fetchone()
            if replay:
                if replay[0] != fingerprint:
                    raise Error(409, "idempotency key reused with a different request")
                return json.loads(replay[1])
            if method == "POST":
                identifier = uuid.uuid4().hex
            else:
                row = db.execute(
                    "SELECT owner,deleted FROM drafts WHERE id=?", (identifier,)
                ).fetchone()
                if row is None or row[1]:
                    raise Error(404, "business not found")
                if row[0] != owner:
                    raise Error(403, "business belongs to another owner")
            if method == "DELETE":
                db.execute("UPDATE drafts SET deleted=1 WHERE id=?", (identifier,))
            else:
                db.execute(
                    "INSERT INTO drafts VALUES(?,?,?,0) ON CONFLICT(id) DO UPDATE SET body=excluded.body,deleted=0",
                    (identifier, owner, canonical(body)),
                )
            response = {
                "id": identifier,
                "status": "pending",
                "effective_after_utc_day": self.day(),
            }
            db.execute(
                "INSERT INTO replay VALUES(?,?,?,?,?)",
                (owner, key, fingerprint, canonical(response), self.clock()),
            )
            return response

    def _install(self, db, businesses):
        db.execute("DELETE FROM public")
        db.execute("DELETE FROM geo")
        for business in businesses:
            identifier = business["id"]
            db.execute(
                "INSERT INTO public VALUES(?,?)", (identifier, canonical(business))
            )
            code = encode(business["latitude"], business["longitude"], 6)
            for precision in range(1, 7):
                cell = code[:precision]
                shard = (
                    int.from_bytes(hashlib.sha256(cell.encode()).digest()[:4], "big")
                    % self.shards
                )
                db.execute(
                    "INSERT INTO geo VALUES(?,?,?,?)",
                    (precision, cell, shard, identifier),
                )

    def publish(self, force=False, fail_hook=None):
        if self.readonly:
            with self.transaction() as db:
                if self.metadata(db)["replica_initialized"] != "1":
                    raise Error(503, "replica has no published snapshot")
            return self.health()
        if not force:
            with self.transaction() as db:
                meta = self.metadata(db)
                if meta["day"] >= self.day():
                    return {
                        "version": int(meta["version"]),
                        "day": meta["day"],
                        "lineage": meta["lineage"],
                    }
        with self.transaction(True) as db:
            return self._publication(db, force, fail_hook)

    def _publication(self, db, force=False, fail_hook=None):
        meta = self.metadata(db)
        if not force and meta["day"] >= self.day():
            return {
                "version": int(meta["version"]),
                "day": meta["day"],
                "lineage": meta["lineage"],
            }
        businesses = [
            dict(json.loads(body), id=identifier)
            for identifier, body in db.execute(
                "SELECT id,body FROM drafts WHERE deleted=0"
            )
        ]
        self._install(db, businesses)
        if fail_hook:
            fail_hook()
        version = int(meta["version"]) + 1
        db.execute("UPDATE meta SET value=? WHERE key='version'", (str(version),))
        db.execute("UPDATE meta SET value=? WHERE key='day'", (self.day(),))
        return {"version": version, "day": self.day(), "lineage": meta["lineage"]}

    def health(self):
        with self.transaction() as db:
            meta = self.metadata(db)
            return {
                "version": int(meta["version"]),
                "day": meta["day"],
                "lineage": meta["lineage"],
                "businesses": db.execute("SELECT count(*) FROM public").fetchone()[0],
                "role": "replica" if self.readonly else "primary",
                "ready": not self.readonly or meta["replica_initialized"] == "1",
                "logical_shards": self.shards,
            }

    def export(self):
        self.publish()
        with self.transaction() as db:
            meta = self.metadata(db)
            return {
                "version": int(meta["version"]),
                "day": meta["day"],
                "lineage": meta["lineage"],
                "businesses": [
                    json.loads(row[0])
                    for row in db.execute("SELECT body FROM public ORDER BY id")
                ],
            }

    def import_snapshot(self, snapshot):
        if not self.readonly:
            raise Error(403, "snapshot import requires replica")
        if (
            not isinstance(snapshot, dict)
            or type(snapshot.get("version")) is not int
            or snapshot["version"] < 0
        ):
            raise ValueError("invalid snapshot version")
        uuid.UUID(snapshot["lineage"])
        datetime.strptime(snapshot["day"], "%Y-%m-%d")
        businesses = []
        for item in snapshot["businesses"]:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item["id"]):
                raise ValueError("invalid snapshot business ID")
            businesses.append(
                dict(
                    validate_business({k: v for k, v in item.items() if k != "id"}),
                    id=item["id"],
                )
            )
        with self.transaction(True) as db:
            meta = self.metadata(db)
            if meta["replica_initialized"] == "1":
                if snapshot["lineage"] != meta["lineage"] or snapshot["version"] < int(
                    meta["version"]
                ):
                    raise Error(409, "foreign lineage or snapshot rollback")
                if snapshot["version"] == int(meta["version"]):
                    return False
            self._install(db, businesses)
            for key in ("version", "day", "lineage"):
                db.execute(
                    "UPDATE meta SET value=? WHERE key=?", (str(snapshot[key]), key)
                )
            db.execute("UPDATE meta SET value='1' WHERE key='replica_initialized'")
            return True

    def detail(self, identifier):
        self.publish()
        with self.transaction() as db:
            meta = self.metadata(db)
            version = meta["version"]
            result = self.details.get(
                (meta["lineage"], version, identifier),
                lambda: self._detail(db, identifier),
            )
            if result is None:
                raise Error(404, "business not found")
            return {"version": int(version), "business": result}

    def _detail(self, db, identifier):
        row = db.execute("SELECT body FROM public WHERE id=?", (identifier,)).fetchone()
        return json.loads(row[0]) if row else None

    def search(
        self,
        latitude,
        longitude,
        radius=5000,
        category=None,
        open_at=None,
        limit=100,
        cursor=None,
    ):
        lat, lon = coordinates(latitude, longitude)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        precision, cells = cover(lat, lon, radius)
        instant = None
        if open_at:
            instant = datetime.fromisoformat(open_at.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                raise ValueError("open_at requires timezone")
            instant = instant.astimezone(timezone.utc)
        fingerprint = hashlib.sha256(
            canonical([lat, lon, radius, category, open_at, limit]).encode()
        ).hexdigest()
        self.publish()
        with self.transaction() as db:
            meta = self.metadata(db)
            version = int(meta["version"])
            offset = 0
            if cursor:
                try:
                    token = json.loads(
                        base64.urlsafe_b64decode(cursor + "=" * ((-len(cursor)) % 4))
                    )
                    if (
                        token["query"] != fingerprint
                        or token["version"] != version
                        or token["lineage"] != meta["lineage"]
                    ):
                        raise Error(409, "cursor query or snapshot changed")
                    offset = token["offset"]
                    if type(offset) is not int or offset < 0:
                        raise ValueError()
                except Error:
                    raise
                except Exception:
                    raise ValueError("invalid cursor")
            ids = set()
            for cell in cells:
                shard = (
                    int.from_bytes(hashlib.sha256(cell.encode()).digest()[:4], "big")
                    % self.shards
                )
                ids.update(
                    self.candidates.get(
                        (meta["lineage"], version, precision, cell),
                        lambda cell=cell, shard=shard: tuple(
                            row[0]
                            for row in db.execute(
                                "SELECT id FROM geo WHERE precision=? AND cell=? AND shard=?",
                                (precision, cell, shard),
                            )
                        ),
                    )
                )
            matches = []
            for identifier in ids:
                business = self.details.get(
                    (meta["lineage"], str(version), identifier),
                    lambda identifier=identifier: self._detail(db, identifier),
                )
                if category and business["category"] != category:
                    continue
                if instant and not any(
                    w["day"] == instant.weekday()
                    and w["start"] <= instant.hour * 60 + instant.minute < w["end"]
                    for w in business["hours"]
                ):
                    continue
                meters = distance(lat, lon, business["latitude"], business["longitude"])
                if meters <= radius + 1e-6:
                    matches.append(dict(business, distance_m=meters))
            matches.sort(key=lambda b: (b["distance_m"], b["id"]))
            next_cursor = None
            if offset + limit < len(matches):
                next_cursor = (
                    base64.urlsafe_b64encode(
                        canonical(
                            {
                                "query": fingerprint,
                                "version": version,
                                "lineage": meta["lineage"],
                                "offset": offset + limit,
                            }
                        ).encode()
                    )
                    .decode()
                    .rstrip("=")
                )
            return {
                "version": version,
                "snapshot_day": meta["day"],
                "total": len(matches),
                "businesses": matches[offset : offset + limit],
                "next_cursor": next_cursor,
                "precision": precision,
                "cells": len(cells),
                "candidates": len(ids),
            }
