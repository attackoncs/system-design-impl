"""Focused persistence operations composed by Store."""

import json

from .common import Error, checksum, encode, identifier, integer
from .contracts import update as validate_update
from .routing import partition


class ProfileMixin:
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
        body = validate_update(body)
        fence = getattr(self, "fence", None)
        if fence is not None:
            fence.assert_held()
        fingerprint = checksum(body)
        published = None
        result = None
        with self.transaction() as db:
            self.check_partition(db, user)
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
            published = seq
        if fence is not None and published is not None:
            fence.publish(published)
        return result

    def get(self, user):
        identifier(user)
        with self.connection() as db:
            self.check_partition(db, user)
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

    def check_partition(self, db, user):
        row = db.execute("SELECT count,partition FROM topology WHERE id=1").fetchone()
        if row and partition(user, row[0]) != row[1]:
            raise Error(409, "wrong database shard")
