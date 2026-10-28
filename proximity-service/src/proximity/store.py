from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid

from .geo import coordinates, cover, distance, encode
from .cache import Cache as Cache
from .models import (
    Error as Error,
    canonical as canonical,
    validate_business as validate_business,
    identifier as valid_identifier,
    query_options,
    cursor_offset,
    next_cursor,
)


class Store:
    def __init__(self, path, shards=4, clock=time.time, readonly=False):
        if type(shards) is not int or not 1 <= shards <= 256:
            raise ValueError("shards must be an integer in 1..256")
        if type(readonly) is not bool or not callable(clock):
            raise ValueError("invalid readonly/clock configuration")
        if (
            not isinstance(path, (str, os.PathLike))
            or not str(path)
            or str(path) == ":memory:"
        ):
            raise ValueError("Store requires a persistent database file")
        self.path, self.shards, self.clock, self.readonly = (
            str(path),
            shards,
            clock,
            readonly,
        )
        self._publish_lock = threading.Lock()
        self.candidates, self.details = Cache(), Cache()
        with closing(self.connect()) as db:
            deadline = time.monotonic() + 15
            while True:
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as error:
                    if (
                        "locked" not in str(error).lower()
                        or time.monotonic() >= deadline
                    ):
                        raise
                    time.sleep(0.01)
            db.executescript("""
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
                "draft_revision": "0",
                "content_hash": "",
            }.items():
                db.execute("INSERT OR IGNORE INTO meta VALUES(?,?)", (key, value))
            self.shards = int(self.metadata(db)["shards"])
            if not 1 <= self.shards <= 256:
                raise ValueError("invalid persisted shard configuration")
            db.commit()
            # Database triggers also cover maintenance tools/direct fixture writes.
            for action in ("INSERT", "UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS drafts_{action.lower()}_revision "
                    f"AFTER {action} ON drafts BEGIN UPDATE meta "
                    "SET value=CAST(value AS INTEGER)+1 WHERE key='draft_revision'; END"
                )
            db.commit()
        self._initialize_content_hash()

    def day(self):
        try:
            value = self.clock()
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError()
            return datetime.fromtimestamp(value, timezone.utc).date().isoformat()
        except (ValueError, OverflowError, OSError):
            raise ValueError("invalid publication clock") from None

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("PRAGMA busy_timeout=15000")
            return db
        except BaseException:
            db.close()
            raise

    @contextmanager
    def transaction(self, write=False):
        if type(write) is not bool:
            raise ValueError("write must be a boolean")
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
        if not isinstance(owner, str) or not 1 <= len(owner) <= 200:
            raise Error(400, "invalid owner")
        if method not in ("POST", "PUT", "DELETE"):
            raise Error(405, "unsupported mutation")
        if (method == "POST") != (identifier is None):
            raise Error(400, "business ID does not match mutation")
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise Error(400, "Idempotency-Key required (1..128 characters)")
        if identifier is not None and (
            not isinstance(identifier, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier)
        ):
            raise Error(400, "invalid business ID")
        if method != "DELETE":
            body = validate_business(body)
        fingerprint = hashlib.sha256(
            canonical([method, identifier, body]).encode()
        ).hexdigest()
        self.publish()
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

    @staticmethod
    def _head(meta):
        return {
            "version": int(meta["version"]),
            "day": meta["day"],
            "lineage": meta["lineage"],
        }

    @staticmethod
    def _content_hash(businesses):
        return hashlib.sha256(
            canonical(sorted(businesses, key=lambda item: item["id"])).encode()
        ).hexdigest()

    def _initialize_content_hash(self):
        # Legacy databases keep the same five tables and published generation.
        with self.transaction() as db:
            meta = self.metadata(db)
            if meta["content_hash"]:
                return
            businesses = [
                json.loads(row[0])
                for row in db.execute("SELECT body FROM public ORDER BY id")
            ]
        digest = self._content_hash(businesses)
        with self.transaction(True) as db:
            current = self.metadata(db)
            if not current["content_hash"] and self._head(current) == self._head(meta):
                db.execute(
                    "UPDATE meta SET value=? WHERE key='content_hash'", (digest,)
                )

    def _prepare(self, businesses):
        """Encode immutable public rows and all geohash precision rows offline."""
        public, geo = [], []
        for business in businesses:
            identifier = business["id"]
            public.append((identifier, canonical(business)))
            code = encode(business["latitude"], business["longitude"], 6)
            for precision in range(1, 7):
                cell = code[:precision]
                shard = (
                    int.from_bytes(hashlib.sha256(cell.encode()).digest()[:4], "big")
                    % self.shards
                )
                geo.append((precision, cell, shard, identifier))
        return public, geo, self._content_hash(businesses)

    def _install(self, db, businesses=None, *, prepared=None):
        # The businesses argument remains for legacy private fixtures. Public
        # publication/import always pass rows prepared outside the writer lock.
        public, geo, digest = (
            self._prepare(businesses) if prepared is None else prepared
        )
        db.execute("DELETE FROM public")
        db.execute("DELETE FROM geo")
        db.executemany("INSERT INTO public VALUES(?,?)", public)
        db.executemany("INSERT INTO geo VALUES(?,?,?,?)", geo)
        db.execute("UPDATE meta SET value=? WHERE key='content_hash'", (digest,))

    def publish(self, force=False, fail_hook=None, *, wait=True):
        """Prepare from a consistent read, then fence one atomic publication.

        JSON/geohash construction runs outside write transactions. Bulk SQL
        replacement still requires the SQLite writer; existing WAL readers keep
        their previous consistent generation until commit.
        """
        if (
            type(force) is not bool
            or type(wait) is not bool
            or (force and not wait)
            or (fail_hook is not None and not callable(fail_hook))
        ):
            raise ValueError("invalid publication options")
        if self.readonly:
            with self.transaction() as db:
                if self.metadata(db)["replica_initialized"] != "1":
                    raise Error(503, "replica has no published snapshot")
            return self.health()
        if not force:
            with self.transaction() as db:
                meta = self.metadata(db)
                if meta["day"] >= self.day():
                    return self._head(meta)
        acquired = self._publish_lock.acquire(blocking=wait)
        if not acquired:
            with self.transaction() as db:
                return self._head(self.metadata(db))
        try:
            for _attempt in range(3):
                target_day = self.day()
                with self.transaction() as db:
                    meta = self.metadata(db)
                    if not force and meta["day"] >= target_day:
                        return self._head(meta)
                    businesses = [
                        dict(json.loads(body), id=identifier)
                        for identifier, body in db.execute(
                            "SELECT id,body FROM drafts WHERE deleted=0 ORDER BY id"
                        )
                    ]
                prepared = self._prepare(businesses)
                with self.transaction(True) as db:
                    current = self.metadata(db)
                    if not force and current["day"] >= target_day:
                        return self._head(current)
                    if (self._head(current), current["draft_revision"]) != (
                        self._head(meta),
                        meta["draft_revision"],
                    ):
                        continue
                    if target_day != self.day():
                        continue
                    self._install(db, prepared=prepared)
                    if fail_hook is not None:
                        fail_hook()
                    version = int(current["version"]) + 1
                    db.execute(
                        "UPDATE meta SET value=? WHERE key='version'", (str(version),)
                    )
                    db.execute("UPDATE meta SET value=? WHERE key='day'", (target_day,))
                    return {
                        "version": version,
                        "day": target_day,
                        "lineage": current["lineage"],
                    }
            raise Error(503, "drafts changed during snapshot build; retry")
        finally:
            self._publish_lock.release()

    def _publication(self, db, force=False, fail_hook=None):
        """Guard mutation acceptance after publish() prepared the day outside SQL."""
        meta = self.metadata(db)
        if force or fail_hook is not None or meta["day"] < self.day():
            raise Error(503, "snapshot publication is due; retry")
        return self._head(meta)

    def health(self):
        with self.transaction() as db:
            meta = self.metadata(db)
            return dict(
                self._head(meta),
                businesses=db.execute("SELECT count(*) FROM public").fetchone()[0],
                role="replica" if self.readonly else "primary",
                ready=not self.readonly or meta["replica_initialized"] == "1",
                logical_shards=self.shards,
            )

    def export(self):
        self.publish()
        with self.transaction() as db:
            meta = self.metadata(db)
            return dict(
                self._head(meta),
                businesses=[
                    json.loads(row[0])
                    for row in db.execute("SELECT body FROM public ORDER BY id")
                ],
            )

    def import_snapshot(self, snapshot):
        """Install a validated immutable generation without mixed content reuse."""
        if not self.readonly:
            raise Error(403, "snapshot import requires replica")
        if not isinstance(snapshot, dict):
            raise ValueError("invalid snapshot shape")
        # Freeze the validated header before potentially slow index encoding.
        # A library caller may retain/reuse its dictionary while this runs;
        # metadata installed after encoding must still be the header validated
        # here. Business records are separately rebuilt into owned values below.
        snapshot = dict(snapshot)
        if (
            set(snapshot) != {"version", "day", "lineage", "businesses"}
            or type(snapshot.get("version")) is not int
            or not 0 <= snapshot["version"] < 2**63
            or not isinstance(snapshot.get("lineage"), str)
            or not isinstance(snapshot.get("day"), str)
            or not isinstance(snapshot.get("businesses"), list)
        ):
            raise ValueError("invalid snapshot shape")
        try:
            if str(uuid.UUID(snapshot["lineage"])) != snapshot["lineage"]:
                raise ValueError()
            if (
                datetime.strptime(snapshot["day"], "%Y-%m-%d").date().isoformat()
                != snapshot["day"]
            ):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError("invalid snapshot lineage/day") from None
        businesses = []
        for item in snapshot["businesses"]:
            if not isinstance(item, dict) or "id" not in item:
                raise ValueError("invalid snapshot business")
            valid_identifier(item["id"])
            businesses.append(
                dict(
                    validate_business({k: v for k, v in item.items() if k != "id"}),
                    id=item["id"],
                )
            )
        prepared = self._prepare(businesses)
        with self._publish_lock, self.transaction(True) as db:
            meta = self.metadata(db)
            if meta["replica_initialized"] == "1":
                if snapshot["lineage"] != meta["lineage"] or snapshot["version"] < int(
                    meta["version"]
                ):
                    raise Error(409, "foreign lineage or snapshot rollback")
                if snapshot["version"] == int(meta["version"]):
                    if (
                        snapshot["day"] != meta["day"]
                        or prepared[2] != meta["content_hash"]
                    ):
                        raise Error(
                            409, "snapshot generation contains different content"
                        )
                    return False
                if snapshot["day"] < meta["day"]:
                    raise Error(409, "snapshot day rollback")
            self._install(db, prepared=prepared)
            for key in ("version", "day", "lineage"):
                db.execute(
                    "UPDATE meta SET value=? WHERE key=?", (str(snapshot[key]), key)
                )
            db.execute("UPDATE meta SET value='1' WHERE key='replica_initialized'")
            return True

    def detail(self, identifier):
        valid_identifier(identifier)
        self.publish(wait=False)
        with self.transaction() as db:
            meta = self.metadata(db)
            version = meta["version"]
            body = self.details.get(
                (meta["lineage"], version, identifier),
                lambda: self._detail(db, identifier),
            )
            if body is None:
                raise Error(404, "business not found")
            return {"version": int(version), "business": json.loads(body)}

    def _detail(self, db, identifier):
        # Cache immutable canonical text. Parsing at the response boundary gives
        # callers isolated nested values without copying a record twice on miss.
        row = db.execute("SELECT body FROM public WHERE id=?", (identifier,)).fetchone()
        return row[0] if row else None

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
        precision, cells = cover(lat, lon, radius)
        instant, fingerprint = query_options(lat, lon, radius, category, open_at, limit)
        self.publish(wait=False)
        with self.transaction() as db:
            meta = self.metadata(db)
            version = int(meta["version"])
            offset = cursor_offset(cursor, fingerprint, version, meta["lineage"])
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
            hydrated = None

            def hydrate(identifier):
                # The first cache miss fetches candidate bodies in bounded SQL
                # batches; later misses reuse this request's immutable snapshot.
                nonlocal hydrated
                if hydrated is None:
                    hydrated = {}
                    ordered = sorted(ids)
                    for start in range(0, len(ordered), 256):
                        batch = ordered[start : start + 256]
                        marks = ",".join("?" for _ in batch)
                        for key, body in db.execute(
                            "SELECT id,body FROM public WHERE id IN (" + marks + ")",
                            batch,
                        ):
                            hydrated[key] = body
                return hydrated.get(identifier)

            for identifier in sorted(ids):
                body = self.details.get(
                    (meta["lineage"], str(version), identifier),
                    lambda identifier=identifier: hydrate(identifier),
                )
                if body is None:
                    raise Error(503, "published index is incomplete")
                business = json.loads(body)
                if category is not None and business["category"] != category:
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
            following = None
            if offset + limit < len(matches):
                following = next_cursor(
                    fingerprint, version, meta["lineage"], offset + limit
                )
            return {
                "version": version,
                "snapshot_day": meta["day"],
                "total": len(matches),
                "businesses": matches[offset : offset + limit],
                "next_cursor": following,
                "precision": precision,
                "cells": len(cells),
                "candidates": len(ids),
            }
