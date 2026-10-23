from bisect import bisect_left
from contextlib import closing, contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit


class Error(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def user_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise Error(400, "invalid user ID")
    return value


def meters(a, b):
    p, q = math.radians(a["latitude"]), math.radians(b["latitude"])
    h = (
        math.sin((q - p) / 2) ** 2
        + math.cos(p)
        * math.cos(q)
        * math.sin(math.radians(b["longitude"] - a["longitude"]) / 2) ** 2
    )
    return 12742017.6 * math.asin(math.sqrt(max(0, min(1, h))))


class Ring:
    def __init__(self, nodes):
        if not isinstance(nodes, dict) or not 1 <= len(nodes) <= 32:
            raise Error(400, "ring requires 1..32 broker ID/URL pairs")
        self.nodes = dict(nodes)
        for node, url in self.nodes.items():
            user_id(node)
            parts = urlsplit(url)
            if parts.scheme not in ("redis", "rediss") or not parts.hostname:
                raise Error(400, "invalid Redis URL")
        self.points = sorted(
            (self.hash(f"{node}:{i}"), node) for node in self.nodes for i in range(64)
        )
        self.tokens = [point[0] for point in self.points]

    @staticmethod
    def hash(value):
        return int.from_bytes(hashlib.sha256(value.encode()).digest(), "big")

    def owner(self, user):
        return self.points[
            bisect_left(self.tokens, self.hash(user)) % len(self.points)
        ][1]


class Store:
    def __init__(self, path, ttl=600, retention=86400, clock=time.time):
        if ttl <= 0 or retention <= 0:
            raise ValueError("positive TTL and retention required")
        self.path, self.clock = str(path), clock
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,token TEXT UNIQUE,admin INTEGER,sharing INTEGER DEFAULT 0,history INTEGER DEFAULT 0,revision INTEGER DEFAULT 0,last_ts REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS requests(sender TEXT,recipient TEXT,PRIMARY KEY(sender,recipient));
            CREATE TABLE IF NOT EXISTS friends(a TEXT,b TEXT,PRIMARY KEY(a,b));
            CREATE TABLE IF NOT EXISTS current(user TEXT PRIMARY KEY,body TEXT,expires REAL);
            CREATE TABLE IF NOT EXISTS history(user TEXT,revision INTEGER,body TEXT,received REAL,PRIMARY KEY(user,revision));
            CREATE INDEX IF NOT EXISTS history_received ON history(received);
            CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY,value TEXT);
            """)
            for key, value in {
                "ttl": ttl,
                "retention": retention,
                "ring": {"epoch": 0, "nodes": {}},
            }.items():
                db.execute(
                    "INSERT OR IGNORE INTO config VALUES(?,?)", (key, dumps(value))
                )
            self.ttl = float(
                json.loads(
                    db.execute("SELECT value FROM config WHERE key='ttl'").fetchone()[0]
                )
            )
            self.retention = float(
                json.loads(
                    db.execute(
                        "SELECT value FROM config WHERE key='retention'"
                    ).fetchone()[0]
                )
            )
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def transaction(self, write=False):
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
        if not isinstance(token, str) or len(token) < 8:
            raise ValueError("token must contain at least 8 characters")
        with self.transaction(True) as db:
            db.execute(
                "INSERT INTO users(id,token,admin) VALUES(?,?,?)",
                (user, hashlib.sha256(token.encode()).hexdigest(), int(admin)),
            )

    def authenticate(self, token):
        with self.transaction() as db:
            row = db.execute(
                "SELECT id FROM users WHERE token=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            if not row:
                raise Error(401, "invalid token")
            return row["id"]

    def _user(self, db, user):
        row = db.execute("SELECT * FROM users WHERE id=?", (user,)).fetchone()
        if not row:
            raise Error(404, "user not found")
        return row

    def friendship(self, actor, target, operation):
        user_id(target)
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
        lat, lon, stamp = (
            body.get("latitude"),
            body.get("longitude"),
            body.get("timestamp"),
        )
        if any(
            type(v) not in (int, float) or not math.isfinite(v)
            for v in (lat, lon, stamp)
        ):
            raise Error(400, "finite numeric coordinates/timestamp required")
        now = self.clock()
        if (
            not -90 <= lat <= 90
            or not -180 <= lon <= 180
            or not now - self.ttl < stamp <= now + 30
        ):
            raise Error(400, "coordinates/timestamp outside allowed range")
        device = user_id(body.get("device", "default"))
        with self.transaction(True) as db:
            user = self._user(db, actor)
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
            self._sweep(db)
        return report

    def _sweep(self, db):
        db.execute("DELETE FROM current WHERE expires<=?", (self.clock(),))
        db.execute(
            "DELETE FROM history WHERE received<=?", (self.clock() - self.retention,)
        )

    def sweep(self):
        with self.transaction(True) as db:
            self._sweep(db)

    def read_history(self, actor, offset=0, limit=100):
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise Error(400, "invalid history page")
        with self.transaction() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT body FROM history WHERE user=? AND received>? ORDER BY revision DESC LIMIT ? OFFSET ?",
                    (actor, self.clock() - self.retention, limit, offset),
                )
            ]

    def reports(self, actor):
        with self.transaction() as db:
            viewer = self._user(db, actor)
            if not viewer["sharing"]:
                return None, []
            own = db.execute(
                "SELECT body FROM current WHERE user=? AND expires>?",
                (actor, self.clock()),
            ).fetchone()
            if not own:
                return None, []
            rows = db.execute(
                """SELECT c.body FROM friends f JOIN users u
                ON u.id=CASE WHEN f.a=? THEN f.b ELSE f.a END
                JOIN current c ON c.user=u.id
                WHERE (f.a=? OR f.b=?) AND u.sharing=1 AND c.expires>?""",
                (actor, actor, actor, self.clock()),
            )
            return json.loads(own[0]), [json.loads(row[0]) for row in rows]

    def ring(self):
        with self.transaction() as db:
            return json.loads(
                db.execute("SELECT value FROM config WHERE key='ring'").fetchone()[0]
            )

    def set_ring(self, actor, nodes, expected_epoch):
        Ring(nodes)
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
