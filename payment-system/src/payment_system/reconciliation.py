import json
import uuid
from .common import Error, digest, dumps, identifier
from .contracts import statement_copy, job_identity


class ReconciliationMixin:
    def review(self, db, op, reason, detail):
        id = digest([op, reason, detail])
        db.execute(
            "INSERT OR IGNORE INTO reviews VALUES(?,?,?,?,'open',?)",
            (id, op, reason, dumps(detail), self.clock()),
        )
        return id

    def schedule_reconcile(self):
        id = uuid.uuid4().hex
        with self.transaction(True) as db:
            db.execute(
                "INSERT INTO reconciliations VALUES(?,'pending',NULL,?)",
                (id, self.clock()),
            )
            self.enqueue(db, "reconcile:" + id, "reconcile", {"run": id})
        return {"run": id, "status": "pending"}

    def reconcile(self, id, statement, job=None):
        identifier(id)
        statement = statement_copy(statement)
        differences = []
        provider = {r["id"]: r for r in statement["operations"]}
        with self.transaction(True) as db:
            run = db.execute(
                "SELECT * FROM reconciliations WHERE id=?", (id,)
            ).fetchone()
            if not run:
                raise Error(404, "reconciliation run missing")
            if run["status"] == "done":
                return json.loads(run["result"])
            if job is not None:
                job_id, token = job_identity(job)
                current = db.execute(
                    "SELECT payload FROM jobs WHERE id=? AND token=? AND status='inflight' AND lease>? AND kind='reconcile'",
                    (job_id, token, self.clock()),
                ).fetchone()
                if not current or json.loads(current["payload"]).get("run") != id:
                    raise Error(409, "reconciliation lease no longer owned")
            for row in db.execute("SELECT * FROM operations ORDER BY id"):
                receipt = provider.pop(row["id"], None)
                reason = None
                if receipt is None and row["status"] == "SUCCEEDED":
                    reason = "MISSING_PROVIDER_TRANSACTION"
                elif receipt:
                    if tuple(
                        row[k]
                        for k in ("merchant", "kind", "parent", "amount", "currency")
                    ) != tuple(
                        receipt[k]
                        for k in ("merchant", "kind", "parent", "amount", "currency")
                    ):
                        reason = "STATEMENT_AMOUNT_OR_IDENTITY_MISMATCH"
                    elif row["status"] != receipt["status"]:
                        reason = "PROVIDER_AHEAD_OR_STATUS_MISMATCH"
                    elif (
                        row["provider_id"]
                        and row["provider_id"] != receipt["provider_id"]
                    ):
                        reason = "PROVIDER_ID_MISMATCH"
                settled = db.execute(
                    "SELECT 1 FROM ledger WHERE operation=? AND kind='settle' AND posted=1",
                    (row["id"],),
                ).fetchone()
                if bool(settled) != (row["status"] == "SUCCEEDED"):
                    reason = "OPERATION_LEDGER_MISMATCH"
                if reason:
                    detail = {"local_status": row["status"], "provider": receipt}
                    differences.append(
                        {
                            "operation": row["id"],
                            "reason": reason,
                            "review": self.review(db, row["id"], reason, detail),
                        }
                    )
            for op, receipt in provider.items():
                differences.append(
                    {
                        "operation": op,
                        "reason": "ORPHAN_PROVIDER_OPERATION",
                        "review": self.review(
                            db, op, "ORPHAN_PROVIDER_OPERATION", receipt
                        ),
                    }
                )
            audit = self.audit(db)
            for error in audit["errors"]:
                differences.append(
                    {
                        **error,
                        "review": self.review(db, "internal", error["reason"], error),
                    }
                )
            result = {
                "matches": not differences,
                "differences": differences,
                "audit": audit,
                "provider_snapshot": statement["snapshot"],
            }
            db.execute(
                "UPDATE reconciliations SET status='done',result=? WHERE id=?",
                (dumps(result), id),
            )
            return result

    def status(self):
        with self.transaction() as db:
            return {
                "operations": db.execute("SELECT count(*) FROM operations").fetchone()[
                    0
                ],
                "pending_jobs": db.execute(
                    "SELECT count(*) FROM jobs WHERE status IN('pending','inflight')"
                ).fetchone()[0],
                "dead_jobs": db.execute(
                    "SELECT count(*) FROM jobs WHERE status='dead'"
                ).fetchone()[0],
                "reviews": [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM reviews WHERE state='open' ORDER BY created LIMIT 100"
                    )
                ],
                "audit": self.audit(db),
            }

    def reconciliation(self, id):
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM reconciliations WHERE id=?", (id,)
            ).fetchone()
            if not row:
                raise Error(404, "reconciliation run missing")
            return {
                "run": id,
                "status": row["status"],
                "result": json.loads(row["result"]) if row["result"] else None,
            }
