"""Transactional friendship, presence and consented location history."""

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from .models import (
    Error as Error,
    dumps as dumps,
    user_id as user_id,
    meters as meters,
    duration,
    finite,
    location,
    page,
)
from .routing import Ring as Ring, redis_url as redis_url
from .schema import SCHEMA


class Store:
    def __init__(self, path, ttl=600, retention=86400, clock=time.time):
        ttl, retention = duration(ttl), duration(retention)
        if (
            not isinstance(path, (str, os.PathLike))
            or not str(path)
            or str(path) == ":memory:"
        ):
            raise ValueError("Store requires a persistent database file")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.path, self.clock = str(path), clock
        self.now()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
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
            db.executescript(SCHEMA)
            for key, value in {
                "ttl": ttl,
                "retention": retention,
                "ring": {"epoch": 0, "nodes": {}},
            }.items():
                db.execute(
                    "INSERT OR IGNORE INTO config VALUES(?,?)", (key, dumps(value))
                )
            self.ttl = duration(
                json.loads(
                    db.execute("SELECT value FROM config WHERE key='ttl'").fetchone()[0]
                )
            )
            self.retention = duration(
                json.loads(
                    db.execute(
                        "SELECT value FROM config WHERE key='retention'"
                    ).fetchone()[0]
                )
            )
            db.commit()

    def now(self):
        value = self.clock()
        if not finite(value) or value < 0:
            raise ValueError("invalid server clock")
        return float(value)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=15000")
            return db
        except BaseException:
            db.close()
            raise

    @contextmanager
    def transaction(self, write=False):
        if type(write) is not bool:
            raise ValueError("write must be a boolean")
        with closing(self.connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def provision(self, user, token, admin=False):
        user_id(user)
        if not isinstance(token, str) or not 8 <= len(token) <= 4096:
            raise ValueError("token must contain 8..4096 characters")
        if type(admin) is not bool:
            raise ValueError("admin must be a boolean")
        with self.transaction(True) as db:
            db.execute(
                "INSERT INTO users(id,token,admin) VALUES(?,?,?)",
                (user, hashlib.sha256(token.encode()).hexdigest(), int(admin)),
            )

    def authenticate(self, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 4096:
            raise Error(401, "invalid token")
        with self.transaction() as db:
            row = db.execute(
                "SELECT id FROM users WHERE token=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            if not row:
                raise Error(401, "invalid token")
            return row["id"]

    def _user(self, db, user):
        user_id(user)
        row = db.execute("SELECT * FROM users WHERE id=?", (user,)).fetchone()
        if not row:
            raise Error(404, "user not found")
        return row

    def friendship(self, actor, target, operation):
        user_id(actor)
        user_id(target)
        if operation not in ("friend.request", "friend.accept", "friend.remove"):
            raise Error(400, "invalid friendship operation")
        if actor == target:
            raise Error(400, "cannot friend yourself")
        a, b = sorted((actor, target))
        with self.transaction(True) as db:
            self._user(db, actor)
            self._user(db, target)
            if operation == "friend.request":
                db.execute(
                    "INSERT OR IGNORE INTO requests VALUES(?,?)", (actor, target)
                )
            elif operation == "friend.accept":
                if not db.execute(
                    "SELECT 1 FROM requests WHERE sender=? AND recipient=?",
                    (target, actor),
                ).fetchone():
                    raise Error(403, "no incoming friendship request")
                count = db.execute(
                    "SELECT count(*) FROM friends WHERE a=? OR b=?", (actor, actor)
                ).fetchone()[0]
                other = db.execute(
                    "SELECT count(*) FROM friends WHERE a=? OR b=?", (target, target)
                ).fetchone()[0]
                if count >= 5000 or other >= 5000:
                    raise Error(409, "friend limit reached")
                db.execute("INSERT OR IGNORE INTO friends VALUES(?,?)", (a, b))
                db.execute(
                    "DELETE FROM requests WHERE (sender=? AND recipient=?) OR (sender=? AND recipient=?)",
                    (a, b, b, a),
                )
            elif operation == "friend.remove":
                db.execute("DELETE FROM friends WHERE a=? AND b=?", (a, b))
                db.execute(
                    "DELETE FROM requests WHERE (sender=? AND recipient=?) OR (sender=? AND recipient=?)",
                    (a, b, b, a),
                )
            else:
                raise Error(400, "invalid friendship operation")
        return {"target": target, "operation": operation}

    def friends(self, actor):
        with self.transaction() as db:
            self._user(db, actor)
            return [
                row[0]
                for row in db.execute(
                    "SELECT CASE WHEN a=? THEN b ELSE a END FROM friends WHERE a=? OR b=? ORDER BY 1",
                    (actor, actor, actor),
                )
            ]

    def setting(self, actor, kind, enabled):
        if type(enabled) is not bool or kind not in ("sharing", "history"):
            raise Error(400, "setting requires boolean enabled")
        with self.transaction(True) as db:
            self._user(db, actor)
            if kind == "sharing":
                db.execute(
                    "UPDATE users SET sharing=?,revision=revision+1 WHERE id=?",
                    (int(enabled), actor),
                )
                if not enabled:
                    db.execute("DELETE FROM current WHERE user=?", (actor,))
            else:
                db.execute(
                    "UPDATE users SET history=? WHERE id=?", (int(enabled), actor)
                )
                if not enabled:
                    db.execute("DELETE FROM history WHERE user=?", (actor,))
        return {kind: enabled}

    def update(self, actor, body):
        user_id(actor)
        lat, lon, stamp, device = location(body)
        with self.transaction(True) as db:
            user = self._user(db, actor)
            # Timestamp admissibility and receipt TTL are measured only after
            # obtaining the writer. A queued report must not acquire a fresh
            # lifetime based on a time sampled before waiting for SQLite.
            now = self.now()
            if not now - self.ttl < stamp <= now + 30:
                raise Error(400, "timestamp outside allowed range")
            if not user["sharing"]:
                raise Error(403, "location sharing disabled")
            if stamp <= user["last_ts"]:
                raise Error(409, "non-increasing user timestamp")
            revision = user["revision"] + 1
            report = {
                "id": actor,
                "latitude": lat,
                "longitude": lon,
                "timestamp": stamp,
                "received_at": now,
                "revision": revision,
                "device": device,
            }
            db.execute(
                "UPDATE users SET revision=?,last_ts=? WHERE id=?",
                (revision, stamp, actor),
            )
            db.execute(
                "INSERT INTO current VALUES(?,?,?) ON CONFLICT(user) DO UPDATE SET body=excluded.body,expires=excluded.expires",
                (actor, dumps(report), now + self.ttl),
            )
            if user["history"]:
                db.execute(
                    "INSERT INTO history VALUES(?,?,?,?)",
                    (actor, revision, dumps(report), now),
                )
        return report

    def _sweep(self, db):
        now = self.now()
        db.execute("DELETE FROM current WHERE expires<=?", (now,))
        db.execute("DELETE FROM history WHERE received<=?", (now - self.retention,))

    def sweep(self):
        with self.transaction(True) as db:
            self._sweep(db)

    def read_history(self, actor, offset=0, limit=100):
        user_id(actor)
        page(offset, limit)
        with self.transaction() as db:
            user = self._user(db, actor)
            if not user["history"]:
                return []
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT body FROM history WHERE user=? AND received>? ORDER BY revision DESC LIMIT ? OFFSET ?",
                    (actor, self.now() - self.retention, limit, offset),
                )
            ]

    def reports(self, actor):
        with self.transaction() as db:
            viewer = self._user(db, actor)
            now = self.now()
            if not viewer["sharing"]:
                return None, []
            own = db.execute(
                "SELECT body FROM current WHERE user=? AND expires>?",
                (actor, now),
            ).fetchone()
            if not own:
                return None, []
            rows = db.execute(
                """SELECT c.body FROM friends f JOIN users u
                ON u.id=CASE WHEN f.a=? THEN f.b ELSE f.a END
                JOIN current c ON c.user=u.id
                WHERE (f.a=? OR f.b=?) AND u.sharing=1 AND c.expires>?""",
                (actor, actor, actor, now),
            )
            return json.loads(own[0]), [json.loads(row[0]) for row in rows]

    def ring(self):
        with self.transaction() as db:
            return json.loads(
                db.execute("SELECT value FROM config WHERE key='ring'").fetchone()[0]
            )

    def set_ring(self, actor, nodes, expected_epoch):
        nodes = dict(Ring(nodes).nodes)
        with self.transaction(True) as db:
            if not self._user(db, actor)["admin"]:
                raise Error(403, "admin required")
            current = json.loads(
                db.execute("SELECT value FROM config WHERE key='ring'").fetchone()[0]
            )
            if type(expected_epoch) is not int or current["epoch"] != expected_epoch:
                raise Error(409, "ring epoch changed")
            value = {"epoch": expected_epoch + 1, "nodes": nodes}
            db.execute("UPDATE config SET value=? WHERE key='ring'", (dumps(value),))
            return {"epoch": value["epoch"]}
