"""Durable SIMULATED payment adapter. No banking APIs or real funds."""

from abc import ABC, abstractmethod
import json
import time

from .common import Database, Error, digest, dumps, http_json
from .payment_contracts import normalize_job, normalize_operation, normalize_receipt


class PaymentAdapter(ABC):
    @abstractmethod
    def execute(self, job):
        """Return a stable receipt keyed by operation ID, or raise on unknown outcome."""

    def execute_with_budget(self, job, *, deadline=None, cancel=None):
        """Compatibility hook; custom adapters must own their blocking I/O budget."""
        return self.execute(job)


class SimulatedHTTPPayment(PaymentAdapter):
    def __init__(self, config):
        owned = json.loads(dumps(config))
        self.url, self.token = owned["payment_url"].rstrip("/"), owned["payment_token"]

    def execute(self, job):
        return self.execute_with_budget(job)

    def execute_with_budget(self, job, *, deadline=None, cancel=None):
        job = normalize_job(job)
        remaining = 5 if deadline is None else deadline - time.monotonic()
        if remaining <= 0 or (cancel is not None and cancel.is_set()):
            raise TimeoutError("payment request expired")
        result = http_json(
            self.url + "/operations",
            self.token,
            {
                k: job[k]
                for k in ("id", "action", "booking_id", "amount", "currency", "method")
            },
            timeout=min(5, remaining),
            deadline=deadline,
            cancel=cancel,
        )
        return normalize_receipt(result, job)


class SimulatedLedger(Database):
    def __init__(self, path):
        super().__init__(path)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,hash TEXT,result TEXT,attempts INTEGER);
        CREATE TABLE IF NOT EXISTS charges(booking TEXT PRIMARY KEY,amount INTEGER,currency TEXT,status TEXT);
        CREATE TABLE IF NOT EXISTS control(id INTEGER PRIMARY KEY CHECK(id=1),fail_after_commit INTEGER,fail_refunds INTEGER);
        INSERT OR IGNORE INTO control VALUES(1,0,0);
        """)

    def control(self, body=None):
        if body is not None:
            if (
                not isinstance(body, dict)
                or set(body) != {"fail_after_commit", "fail_refunds"}
                or any(type(value) is not bool for value in body.values())
            ):
                raise ValueError(
                    "control requires boolean fail_after_commit and fail_refunds"
                )
            body = dict(body)
        with self.transaction(True) as db:
            if body is not None:
                db.execute(
                    "UPDATE control SET fail_after_commit=?,fail_refunds=?",
                    (body["fail_after_commit"], body["fail_refunds"]),
                )
            return dict(db.execute("SELECT * FROM control").fetchone())

    def execute(self, body):
        body = normalize_operation(body)
        fingerprint = digest(body)
        with self.transaction(True) as db:
            control = db.execute("SELECT * FROM control").fetchone()
            old = db.execute(
                "SELECT * FROM operations WHERE id=?", (body["id"],)
            ).fetchone()
            if old:
                if old["hash"] != fingerprint:
                    raise Error(409, "provider idempotency conflict")
                db.execute(
                    "UPDATE operations SET attempts=attempts+1 WHERE id=?",
                    (body["id"],),
                )
                result = json.loads(old["result"])
            else:
                if body["action"] == "refund" and control["fail_refunds"]:
                    raise Error(503, "simulated refund provider unavailable")
                if body["action"] == "charge":
                    if db.execute(
                        "SELECT 1 FROM charges WHERE booking=?", (body["booking_id"],)
                    ).fetchone():
                        raise Error(409, "charge exists without an operation receipt")
                    status = "declined" if body["method"] == "test-decline" else "paid"
                    db.execute(
                        "INSERT INTO charges VALUES(?,?,?,?)",
                        (body["booking_id"], body["amount"], "USD", status),
                    )
                else:
                    charge = db.execute(
                        "SELECT * FROM charges WHERE booking=?", (body["booking_id"],)
                    ).fetchone()
                    if (
                        not charge
                        or charge["status"] != "paid"
                        or charge["amount"] != body["amount"]
                        or charge["currency"] != body["currency"]
                    ):
                        raise Error(409, "refund requires matching paid charge")
                    db.execute(
                        "UPDATE charges SET status='refunded' WHERE booking=?",
                        (body["booking_id"],),
                    )
                    status = "refunded"
                result = {
                    "booking_id": body["booking_id"],
                    "amount": body["amount"],
                    "currency": "USD",
                    "status": status,
                    "simulated": True,
                }
                db.execute(
                    "INSERT INTO operations VALUES(?,?,?,1)",
                    (body["id"], fingerprint, dumps(result)),
                )
            fail = control["fail_after_commit"]
        # This error occurs AFTER durable operation commit, emulating a lost ACK.
        if fail:
            raise Error(503, "simulated response lost after durable payment commit")
        return result

    def status(self):
        with self.transaction() as db:
            total_charges = db.execute("SELECT count(*) FROM charges").fetchone()[0]
            return {
                "simulated": True,
                "operations": db.execute("SELECT count(*) FROM operations").fetchone()[
                    0
                ],
                "attempts": db.execute(
                    "SELECT coalesce(sum(attempts),0) FROM operations"
                ).fetchone()[0],
                "charges": [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM charges ORDER BY booking LIMIT 1000"
                    )
                ],
                "total_charges": total_charges,
                "charges_truncated": total_charges > 1000,
            }
