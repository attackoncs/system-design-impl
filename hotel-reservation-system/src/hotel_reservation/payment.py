"""Durable SIMULATED payment adapter. No banking APIs or real funds."""
from abc import ABC, abstractmethod
import json

from .common import Database, Error, digest, dumps, http_json, integer


class PaymentAdapter(ABC):
    @abstractmethod
    def execute(self, job):
        """Return a stable receipt keyed by operation ID, or raise on unknown outcome."""


class SimulatedHTTPPayment(PaymentAdapter):
    def __init__(self, config):
        self.url, self.token = config["payment_url"], config["payment_token"]

    def execute(self, job):
        return http_json(self.url + "/operations", self.token,
                         {k: job[k] for k in ("id", "action", "booking_id", "amount", "currency", "method")})


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
        with self.transaction(True) as db:
            if body is not None:
                if not isinstance(body, dict) or set(body) != {"fail_after_commit", "fail_refunds"} or any(type(v) is not bool for v in body.values()):
                    raise ValueError("control requires boolean fail_after_commit and fail_refunds")
                db.execute("UPDATE control SET fail_after_commit=?,fail_refunds=?", (body["fail_after_commit"], body["fail_refunds"]))
            return dict(db.execute("SELECT * FROM control").fetchone())

    def execute(self, body):
        if not isinstance(body, dict) or set(body) != {"id", "action", "booking_id", "amount", "currency", "method"}:
            raise ValueError("invalid simulated operation fields")
        if body["action"] not in ("charge", "refund") or body["id"] != body["action"] + ":" + body["booking_id"]:
            raise ValueError("operation ID must match action and reservation")
        integer(body["amount"], "amount", 0, 30_000_000_000)
        if body["currency"] != "USD" or body["method"] not in ("test-ok", "test-decline"):
            raise ValueError("only USD simulated payment methods are supported")
        with self.transaction(True) as db:
            control = db.execute("SELECT * FROM control").fetchone()
            if body["action"] == "refund" and control["fail_refunds"]:
                raise Error(503, "simulated refund provider unavailable")
            old = db.execute("SELECT * FROM operations WHERE id=?", (body["id"],)).fetchone()
            if old:
                if old["hash"] != digest(body):
                    raise Error(409, "provider idempotency conflict")
                db.execute("UPDATE operations SET attempts=attempts+1 WHERE id=?", (body["id"],))
                result = json.loads(old["result"])
            else:
                if body["action"] == "charge":
                    status = "declined" if body["method"] == "test-decline" else "paid"
                    db.execute("INSERT INTO charges VALUES(?,?,?,?)", (body["booking_id"], body["amount"], "USD", status))
                else:
                    charge = db.execute("SELECT * FROM charges WHERE booking=?", (body["booking_id"],)).fetchone()
                    if not charge or charge["status"] != "paid" or charge["amount"] != body["amount"]:
                        raise Error(409, "refund requires matching paid charge")
                    db.execute("UPDATE charges SET status='refunded' WHERE booking=?", (body["booking_id"],))
                    status = "refunded"
                result = {"booking_id": body["booking_id"], "amount": body["amount"], "currency": "USD", "status": status, "simulated": True}
                db.execute("INSERT INTO operations VALUES(?,?,?,1)", (body["id"], digest(body), dumps(result)))
            fail = control["fail_after_commit"]
        # This error occurs AFTER durable operation commit, emulating a lost ACK.
        if fail:
            raise Error(503, "simulated response lost after durable payment commit")
        return result

    def status(self):
        with self.transaction() as db:
            return {"simulated": True, "operations": db.execute("SELECT count(*) FROM operations").fetchone()[0],
                    "attempts": db.execute("SELECT coalesce(sum(attempts),0) FROM operations").fetchone()[0],
                    "charges": [dict(r) for r in db.execute("SELECT * FROM charges ORDER BY booking LIMIT 1000")]}
