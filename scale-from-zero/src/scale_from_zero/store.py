import hashlib
import json
import secrets
import time
import threading

from .common import Database, Error, checksum, encode, identifier, integer


def partition(user, count):
    return int(hashlib.sha256(identifier(user).encode()).hexdigest(), 16) % count


class Store(Database):
    def __init__(self, path, clock=time.time):
        super().__init__(path)
        self.clock = clock
        self.initialize("""
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,version INTEGER,profile TEXT);
        CREATE TABLE IF NOT EXISTS requests(user TEXT,id TEXT,hash TEXT,result TEXT,PRIMARY KEY(user,id));
        CREATE TABLE IF NOT EXISTS changes(seq INTEGER PRIMARY KEY,user TEXT,version INTEGER,profile TEXT,hash TEXT);
        CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,user TEXT,expires REAL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,payload TEXT,state TEXT,lease REAL,token TEXT,attempts INTEGER);
        """)
        self._reads = 0
        self._metrics_lock = threading.Lock()

    @property
    def reads(self):
        with self._metrics_lock:
            return self._reads

    def bind_partition(self, count, index):
        """Reject in-place routing changes; an actual reshard needs data migration."""
        integer(count, 1, 2)
        integer(index, 0, count - 1)
        self.initialize(
            "CREATE TABLE IF NOT EXISTS topology(id INTEGER PRIMARY KEY CHECK(id=1),count INTEGER,partition INTEGER);"
        )
        with self.transaction() as db:
            old = db.execute("SELECT count,partition FROM topology").fetchone()
            if old and tuple(old) != (count, index):
                raise ValueError(
                    "partition map changed; start a fresh lab or implement migration"
                )
            db.execute("INSERT OR IGNORE INTO topology VALUES(1,?,?)", (count, index))

    def update(self, user, body):
        identifier(user)
        if not isinstance(body, dict) or set(body) != {
            "request_id",
            "expected_version",
            "display_name",
            "city",
        }:
            raise ValueError("exact profile fields required")
        identifier(body["request_id"])
        integer(body["expected_version"])
        if any(
            not isinstance(body[key], str) or len(body[key]) > 128
            for key in ("display_name", "city")
        ):
            raise ValueError("profile strings are limited to 128 characters")
        fingerprint = checksum(body)
        with self.transaction() as db:
            old = db.execute(
                "SELECT * FROM requests WHERE user=? AND id=?",
                (user, body["request_id"]),
            ).fetchone()
            if old:
                if old["hash"] != fingerprint:
                    raise Error(409, "request key changed content")
                return json.loads(old["result"])
            old = db.execute("SELECT version FROM users WHERE id=?", (user,)).fetchone()
            version = old[0] if old else 0
            if version != body["expected_version"]:
                raise Error(409, "profile version conflict")
            seq = db.execute("SELECT coalesce(max(seq),0)+1 FROM changes").fetchone()[0]
            if seq > 1000:
                raise Error(429, "reference history limit reached")
            profile = {key: body[key] for key in ("display_name", "city")}
            result = {"user": user, "version": version + 1, "profile": profile}
            db.execute(
                "INSERT OR REPLACE INTO users VALUES(?,?,?)",
                (user, version + 1, encode(profile)),
            )
            change = {"seq": seq, **result}
            db.execute(
                "INSERT INTO changes VALUES(?,?,?,?,?)",
                (seq, user, version + 1, encode(profile), checksum(change)),
            )
            db.execute(
                "INSERT INTO requests VALUES(?,?,?,?)",
                (user, body["request_id"], fingerprint, encode(result)),
            )
            db.execute(
                "INSERT INTO jobs VALUES(?,?,'pending',0,'',0)",
                (user + "_" + str(version + 1), encode(change)),
            )
            return result

    def get(self, user):
        with self.connection() as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (user,)).fetchone()
            with self._metrics_lock:
                self._reads += 1
            if not row:
                raise Error(404, "profile missing")
            return {
                "user": user,
                "version": row["version"],
                "profile": json.loads(row["profile"]),
            }

    def session_create(self, user):
        token = user + "." + secrets.token_hex(24)
        with self.transaction() as db:
            db.execute("DELETE FROM sessions WHERE expires<=?", (self.clock(),))
            if db.execute("SELECT count(*) FROM sessions").fetchone()[0] >= 1000:
                raise Error(429, "reference session capacity reached")
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), user, self.clock() + 3600),
            )
        return {"session": token, "expires_in_seconds": 3600}

    def session_check(self, user, token):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            if not row or row["user"] != user or row["expires"] <= self.clock():
                raise Error(401, "session invalid or expired")
        return {"user": user}

    def changes(self, after):
        integer(after)
        with self.connection() as db:
            return {
                "changes": [
                    {
                        "seq": row["seq"],
                        "user": row["user"],
                        "version": row["version"],
                        "profile": json.loads(row["profile"]),
                        "hash": row["hash"],
                    }
                    for row in db.execute(
                        "SELECT * FROM changes WHERE seq>? ORDER BY seq LIMIT 100",
                        (after,),
                    )
                ]
            }

    def replicate(self, change):
        data = {key: change[key] for key in ("seq", "user", "version", "profile")}
        if checksum(data) != change["hash"]:
            raise Error(409, "replication checksum mismatch")
        with self.transaction() as db:
            current = db.execute("SELECT coalesce(max(seq),0) FROM changes").fetchone()[
                0
            ]
            if change["seq"] <= current:
                old = db.execute(
                    "SELECT hash FROM changes WHERE seq=?", (change["seq"],)
                ).fetchone()
                if not old or old[0] != change["hash"]:
                    raise Error(409, "replica prefix conflict")
                return
            if change["seq"] != current + 1:
                raise Error(409, "replica sequence gap")
            db.execute(
                "INSERT OR REPLACE INTO users VALUES(?,?,?)",
                (change["user"], change["version"], encode(change["profile"])),
            )
            db.execute(
                "INSERT INTO changes VALUES(?,?,?,?,?)",
                (
                    change["seq"],
                    change["user"],
                    change["version"],
                    encode(change["profile"]),
                    change["hash"],
                ),
            )

    def claim(self):
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE state='pending' OR (state='inflight' AND lease<=?) ORDER BY id LIMIT 1",
                (self.clock(),),
            ).fetchone()
            if not row:
                return None
            token = secrets.token_hex(16)
            db.execute(
                "UPDATE jobs SET state='inflight',lease=?,token=?,attempts=attempts+1 WHERE id=?",
                (self.clock() + 1, token, row["id"]),
            )
            return {
                "id": row["id"],
                "token": token,
                "payload": json.loads(row["payload"]),
            }

    def ack(self, job):
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE jobs SET state='done' WHERE id=? AND token=? AND state='inflight'",
                (job["id"], job["token"]),
            ).rowcount
            return {"acknowledged": bool(changed)}

    def status(self):
        with self.connection() as db:
            return {
                "users": db.execute("SELECT count(*) FROM users").fetchone()[0],
                "seq": db.execute(
                    "SELECT coalesce(max(seq),0) FROM changes"
                ).fetchone()[0],
                "profile_reads": self.reads,
                "pending_jobs": db.execute(
                    "SELECT count(*) FROM jobs WHERE state!='done'"
                ).fetchone()[0],
            }


class Consumer(Database):
    def __init__(self, path):
        super().__init__(path)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY,hash TEXT,payload TEXT,attempts INTEGER);
        CREATE TABLE IF NOT EXISTS control(id INTEGER PRIMARY KEY CHECK(id=1),lose_ack INTEGER);
        INSERT OR IGNORE INTO control VALUES(1,0);
        """)

    def consume(self, job):
        with self.transaction() as db:
            old = db.execute("SELECT * FROM inbox WHERE id=?", (job["id"],)).fetchone()
            if old and old["hash"] != checksum(job["payload"]):
                raise Error(409, "consumer id reused with another payload")
            if old:
                db.execute(
                    "UPDATE inbox SET attempts=attempts+1 WHERE id=?", (job["id"],)
                )
            else:
                db.execute(
                    "INSERT INTO inbox VALUES(?,?,?,1)",
                    (job["id"], checksum(job["payload"]), encode(job["payload"])),
                )
            return not bool(db.execute("SELECT lose_ack FROM control").fetchone()[0])

    def control(self, lose_ack):
        if type(lose_ack) is not bool:
            raise ValueError("lose_ack must be boolean")
        with self.transaction() as db:
            db.execute("UPDATE control SET lose_ack=?", (lose_ack,))
        return {"updated": True}

    def status(self):
        with self.connection() as db:
            return {
                "unique_events": db.execute("SELECT count(*) FROM inbox").fetchone()[0],
                "attempts": db.execute(
                    "SELECT coalesce(sum(attempts),0) FROM inbox"
                ).fetchone()[0],
            }
