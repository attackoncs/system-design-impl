"""Source-bound, prefix-checked profile-only replication with atomic batches."""

import json

from .common import Error, integer, checksum, encode
from .contracts import change as validate_change, fields, freeze


class ReplicationMixin:
    def changes(self, after):
        integer(after)
        with self.read_transaction() as db:
            tail = db.execute(
                "SELECT seq,hash FROM changes ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            count, head = (tail[0], tail[1]) if tail else (0, "0" * 64)
            if after > count:
                raise Error(409, "replica cursor ahead of source")
            previous = (
                db.execute("SELECT hash FROM changes WHERE seq=?", (after,)).fetchone()
                if after
                else None
            )
            bound = db.execute(
                "SELECT source_id FROM replication_source WHERE id=1"
            ).fetchone()
            source_id = (
                bound[0]
                if bound
                else db.execute("SELECT value FROM identity WHERE id=1").fetchone()[0]
            )
            rows = [
                {
                    "seq": row["seq"],
                    "user": row["user"],
                    "version": row["version"],
                    "profile": json.loads(row["profile"]),
                    "hash": row["hash"],
                }
                for row in db.execute(
                    "SELECT * FROM changes WHERE seq>? ORDER BY seq LIMIT 100", (after,)
                )
            ]
            return {
                "changes": rows,
                "source_id": source_id,
                "after": after,
                "previous_hash": previous[0] if previous else "0" * 64,
                "count": count,
                "head": head,
            }

    def apply_feed(self, feed):
        feed = freeze(feed)
        fields(
            feed, {"changes", "source_id", "after", "previous_hash", "count", "head"}
        )
        integer(feed["after"])
        integer(feed["count"], feed["after"], 1000)
        if (
            not isinstance(feed["source_id"], str)
            or len(feed["source_id"]) != 32
            or any(char not in "0123456789abcdef" for char in feed["source_id"])
        ):
            raise ValueError("source database identity required")
        if not isinstance(feed["changes"], list) or len(feed["changes"]) > 100:
            raise ValueError("bounded changes list required")
        changes = [validate_change(row) for row in feed["changes"]]
        if [row["seq"] for row in changes] != list(
            range(feed["after"] + 1, min(feed["count"], feed["after"] + 100) + 1)
        ):
            raise Error(409, "source omitted or reordered changes")
        with self.transaction() as db:
            tail = db.execute(
                "SELECT seq,hash FROM changes ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            count, head = (tail[0], tail[1]) if tail else (0, "0" * 64)
            old = db.execute(
                "SELECT source_id FROM replication_source WHERE id=1"
            ).fetchone()
            if old and old[0] != feed["source_id"]:
                raise Error(
                    409, "replication source identity changed; migration required"
                )
            if feed["after"] != count or feed["previous_hash"] != head:
                raise Error(
                    409, "replication source prefix differs from retained replica"
                )
            for change in changes:
                self._replicate(db, change)
            if (
                feed["count"] <= feed["after"] + 100
                and (changes[-1]["hash"] if changes else head) != feed["head"]
            ):
                raise Error(409, "replication head mismatch")
            db.execute(
                "INSERT OR IGNORE INTO replication_source VALUES(1,?)",
                (feed["source_id"],),
            )
        return {"applied": len(changes)}

    def replicate(self, change):
        try:
            change = validate_change(change)
        except ValueError:
            raise Error(409, "invalid replication record") from None
        data = {key: change[key] for key in ("seq", "user", "version", "profile")}
        if checksum(data) != change["hash"]:
            raise Error(409, "replication checksum mismatch")
        with self.transaction() as db:
            self._replicate(db, change)

    def _replicate(self, db, change):
        self.check_partition(db, change["user"])
        current = db.execute("SELECT coalesce(max(seq),0) FROM changes").fetchone()[0]
        if change["seq"] <= current:
            old = db.execute(
                "SELECT hash FROM changes WHERE seq=?", (change["seq"],)
            ).fetchone()
            if not old or old[0] != change["hash"]:
                raise Error(409, "replica prefix conflict")
            return
        if change["seq"] != current + 1:
            raise Error(409, "replica sequence gap")
        old = db.execute(
            "SELECT version FROM users WHERE id=?", (change["user"],)
        ).fetchone()
        if change["version"] != (old[0] + 1 if old else 1):
            raise Error(409, "replica user version gap or regression")
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
