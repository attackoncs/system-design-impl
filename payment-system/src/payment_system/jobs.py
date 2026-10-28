import json
import uuid
from .common import Error, dumps, integer
from .contracts import job_identity


class JobMixin:
    def enqueue(self, db, id, kind, payload):
        db.execute(
            "INSERT OR IGNORE INTO jobs VALUES(?,?,?,'pending',0,0,?,0,'','')",
            (id, kind, dumps(payload), self.clock()),
        )

    def claim(self, lease_seconds=2):
        integer(lease_seconds, "lease seconds", 1, 60)
        with self.transaction(True) as db:
            now = self.clock()
            row = db.execute(
                "SELECT * FROM jobs WHERE (status='pending' AND due<=?) OR (status='inflight' AND lease<=?) ORDER BY due,id LIMIT 1",
                (now, now),
            ).fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE jobs SET status='inflight',attempts=attempts+1,lease=?,token=? WHERE id=?",
                (now + lease_seconds, token, row["id"]),
            )
            db.execute(
                "INSERT INTO executions(job,action,detail,created) VALUES(?,'claim',?,?)",
                (row["id"], token, now),
            )
            return {
                **dict(row),
                "token": token,
                "lease": now + lease_seconds,
                "attempts": row["attempts"] + 1,
                "payload": json.loads(row["payload"]),
            }

    def finish_job(self, job, pending=False, error=None):
        job_id, token = job_identity(job)
        if type(pending) is not bool or (
            error is not None and (not isinstance(error, str) or len(error) > 1000)
        ):
            raise ValueError("invalid job outcome")
        with self.transaction(True) as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE id=? AND token=? AND status='inflight' AND lease>?",
                (job_id, token, self.clock()),
            ).fetchone()
            if not row:
                return False
            failures = row["failures"] + int(error is not None)
            payload = json.loads(row["payload"])
            status = "pending" if pending or error is not None else "done"
            delay = (
                0.2
                if pending and error is None
                else min(30, 0.1 * 2 ** min(failures, 8))
            )
            if error is not None and row["kind"] == "execute":
                db.execute(
                    "UPDATE operations SET status='UNKNOWN' WHERE id=? AND status NOT IN('SUCCEEDED','FAILED')",
                    (payload["operation"],),
                )
            if failures >= 5:
                status = "dead"
                self.review(
                    db,
                    payload.get("operation", row["id"]),
                    "RETRY_EXHAUSTED",
                    {"job": row["id"], "error": str(error)[:300]},
                )
                if row["kind"] == "reconcile":
                    db.execute(
                        "UPDATE reconciliations SET status='failed',result=? WHERE id=?",
                        (dumps({"error": str(error)[:300]}), payload["run"]),
                    )
            db.execute(
                "UPDATE jobs SET status=?,failures=?,due=?,error=?,lease=0,token='' WHERE id=?",
                (
                    status,
                    failures,
                    self.clock() + delay,
                    str(error)[:300] if error else "",
                    row["id"],
                ),
            )
            db.execute(
                "INSERT INTO executions(job,action,detail,created) VALUES(?,?,?,?)",
                (row["id"], status, str(error)[:300] if error else "", self.clock()),
            )
        return True

    def retry(self, op):
        with self.transaction(True) as db:
            value = self.operation(op, db=db)
            if value["status"] in ("SUCCEEDED", "FAILED"):
                raise Error(409, "terminal operation cannot execute again")
            db.execute(
                "UPDATE jobs SET status='pending',failures=0,due=?,lease=0,token='' WHERE id=?",
                (self.clock(), "execute:" + op),
            )
            return {"retry_requested": op}

    def retry_job(self, id):
        """Requeue a dead job with its original operation/event identity."""
        with self.transaction(True) as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (id,)).fetchone()
            if not row:
                raise Error(404, "job missing")
            if row["status"] != "dead":
                raise Error(409, "only dead jobs can be manually requeued")
            db.execute(
                "UPDATE jobs SET status='pending',failures=0,due=?,lease=0,token='',error='' WHERE id=?",
                (self.clock(), id),
            )
            if row["kind"] == "reconcile":
                db.execute(
                    "UPDATE reconciliations SET status='pending',result=NULL WHERE id=?",
                    (json.loads(row["payload"])["run"],),
                )
        return {"retry_requested": id}
