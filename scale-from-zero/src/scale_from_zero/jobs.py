"""Focused persistence operations composed by Store."""

import json
import secrets
from .common import integer
from .contracts import job as validate_job


class JobMixin:
    def claim(self, lease_seconds=1):
        integer(lease_seconds, 1, 60)
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE state='pending' OR (state='inflight' AND lease<=?) ORDER BY id LIMIT 1",
                (self.now(),),
            ).fetchone()
            if not row:
                return None
            token = secrets.token_hex(16)
            db.execute(
                "UPDATE jobs SET state='inflight',lease=?,token=?,attempts=attempts+1 WHERE id=?",
                (self.now() + lease_seconds, token, row["id"]),
            )
            return {
                "id": row["id"],
                "token": token,
                "payload": json.loads(row["payload"]),
                "lease": self.now() + lease_seconds,
                "attempts": row["attempts"] + 1,
                "source_id": db.execute(
                    "SELECT value FROM identity WHERE id=1"
                ).fetchone()[0],
            }

    def ack(self, job):
        job = validate_job(job)
        with self.transaction() as db:
            old = db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
            if (
                job["source_id"]
                != db.execute("SELECT value FROM identity WHERE id=1").fetchone()[0]
            ):
                return {"acknowledged": False}
            if (
                old is None
                or json.loads(old["payload"]) != job["payload"]
                or old["lease"] != job["lease"]
                or old["attempts"] != job["attempts"]
            ):
                return {"acknowledged": False}
            changed = db.execute(
                "UPDATE jobs SET state='done',token='',lease=0 WHERE id=? AND token=? AND state='inflight' AND lease>?",
                (job["id"], job["token"], self.now()),
            ).rowcount
            return {"acknowledged": bool(changed)}
