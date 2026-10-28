"""Saga operations for the single hotel-shard owner."""

import json
import math
import uuid

from .common import Error, dumps, integer
from .payment_contracts import normalize_job, normalize_receipt


class Saga:
    def emit(self, db, booking, kind, payload=None, *, now=None):
        db.execute(
            "INSERT INTO events(booking,kind,payload,created) VALUES(?,?,?,?)",
            (booking, kind, dumps(payload or {}), self._now() if now is None else now),
        )

    def add_job(self, db, booking, action, *, now=None):
        db.execute(
            "INSERT OR IGNORE INTO jobs VALUES(?,?,?,'pending',0,?,0,'','')",
            (
                action + ":" + booking,
                booking,
                action,
                self._now() if now is None else now,
            ),
        )

    def sweep(self):
        with self.transaction(True) as db:
            now = self._now()
            rows = db.execute(
                "SELECT * FROM bookings WHERE status='PENDING' AND expires<=? ORDER BY expires,id LIMIT 100",
                (now,),
            ).fetchall()
            for row in rows:
                self.release(db, row, "EXPIRED", now=now)
            return len(rows)

    def claim(self, lease_seconds=2):
        if (
            type(lease_seconds) not in (int, float)
            or not 0.01 <= lease_seconds <= 3600
            or not math.isfinite(lease_seconds)
        ):
            raise ValueError("lease must be finite and in 0.01..3600 seconds")
        with self.transaction(True) as db:
            now = self._now()
            row = db.execute(
                "SELECT * FROM jobs WHERE (status='pending' AND due<=?) OR (status='inflight' AND lease<=?) ORDER BY due,id LIMIT 1",
                (now, now),
            ).fetchone()
            if not row:
                return None
            booking = db.execute(
                "SELECT * FROM bookings WHERE id=?", (row["booking"],)
            ).fetchone()
            if (
                row["action"] == "charge"
                and row["attempts"] == 0
                and booking["status"] == "PENDING"
                and booking["expires"] <= now
            ):
                self.release(db, booking, "EXPIRED", now=now)
                booking = db.execute(
                    "SELECT * FROM bookings WHERE id=?", (row["booking"],)
                ).fetchone()
            if (
                row["action"] == "charge"
                and row["attempts"] == 0
                and booking["status"] != "PENDING"
            ):
                db.execute("UPDATE jobs SET status='done' WHERE id=?", (row["id"],))
                self.emit(db, booking["id"], "UNCHARGED", now=now)
                return None
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE jobs SET status='inflight',attempts=attempts+1,lease=?,token=? WHERE id=?",
                (now + lease_seconds, token, row["id"]),
            )
            if row["action"] == "charge":
                db.execute(
                    "UPDATE bookings SET payment='CHARGING' WHERE id=? AND payment='NONE'",
                    (booking["id"],),
                )
            return {
                "id": row["id"],
                "token": token,
                "action": row["action"],
                "booking_id": booking["id"],
                "amount": booking["amount"],
                "currency": booking["currency"],
                "method": booking["method"],
            }

    def retry(self, job, message):
        job = normalize_job(job)
        if not isinstance(message, str):
            raise ValueError("retry error must be text")
        message = message[:300]
        with self.transaction(True) as db:
            row = db.execute(
                "SELECT j.attempts,j.booking,j.action,b.amount,b.currency,b.method FROM jobs j JOIN bookings b ON b.id=j.booking WHERE j.id=? AND j.token=? AND j.status='inflight'",
                (job["id"], job["token"]),
            ).fetchone()
            if row:
                if (
                    row["booking"],
                    row["action"],
                    row["amount"],
                    row["currency"],
                    row["method"],
                ) != (
                    job["booking_id"],
                    job["action"],
                    job["amount"],
                    job["currency"],
                    job["method"],
                ):
                    raise Error(409, "job differs from durable payment intent")
                delay = min(30, 0.1 * 2 ** min(row["attempts"], 8))
                db.execute(
                    "UPDATE jobs SET status='pending',due=?,error=? WHERE id=?",
                    (self._now() + delay, message, job["id"]),
                )

    def finish(self, job, result):
        job = normalize_job(job)
        result = normalize_receipt(result, job)
        with self.transaction(True) as db:
            active = db.execute(
                "SELECT * FROM jobs WHERE id=? AND status='inflight' AND token=?",
                (job["id"], job["token"]),
            ).fetchone()
            if not active:
                return False  # Lease fencing: stale worker cannot settle a newer claim.
            row = db.execute(
                "SELECT * FROM bookings WHERE id=?", (active["booking"],)
            ).fetchone()
            if (
                active["booking"],
                active["action"],
                row["amount"],
                row["currency"],
                row["method"],
            ) != (
                job["booking_id"],
                job["action"],
                job["amount"],
                job["currency"],
                job["method"],
            ):
                raise Error(409, "job differs from durable payment intent")
            now = self._now()
            if job["action"] == "charge":
                if result["status"] == "declined":
                    if row["status"] == "PENDING":
                        self.release(db, row, "REJECTED", now=now)
                    db.execute(
                        "UPDATE bookings SET payment='DECLINED' WHERE id=?",
                        (row["id"],),
                    )
                else:
                    if row["status"] == "PENDING" and row["expires"] > now:
                        db.execute(
                            "UPDATE bookings SET status='CONFIRMED',payment='PAID' WHERE id=?",
                            (row["id"],),
                        )
                        self.emit(db, row["id"], "CONFIRMED", now=now)
                    else:
                        if row["status"] == "PENDING":
                            self.release(db, row, "EXPIRED", now=now)
                        db.execute(
                            "UPDATE bookings SET payment='REFUND_PENDING' WHERE id=?",
                            (row["id"],),
                        )
                        self.add_job(db, row["id"], "refund", now=now)
                        self.emit(db, row["id"], "COMPENSATION_REQUIRED", now=now)
            else:
                db.execute(
                    "UPDATE bookings SET payment='REFUNDED' WHERE id=?", (row["id"],)
                )
                self.emit(db, row["id"], "REFUNDED", now=now)
            db.execute(
                "UPDATE jobs SET status='done',error='' WHERE id=?", (job["id"],)
            )
        return True

    def status(self):
        with self.transaction() as db:
            return {
                "shard": self.shard,
                "shards": self.shards,
                "revision": db.execute("SELECT revision FROM metadata").fetchone()[0],
                "bookings": db.execute("SELECT count(*) FROM bookings").fetchone()[0],
                "unfinished_jobs": db.execute(
                    "SELECT count(*) FROM jobs WHERE status!='done'"
                ).fetchone()[0],
                "job_errors": [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,attempts,error FROM jobs WHERE status!='done' AND error!='' ORDER BY due LIMIT 20"
                    )
                ],
                "expired_pending": db.execute(
                    "SELECT count(*) FROM bookings WHERE status='PENDING' AND expires<=?",
                    (self._now(),),
                ).fetchone()[0],
            }

    def events(self, after=0):
        integer(after, "after", 0, 2**53)
        with self.transaction() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT 100", (after,)
                )
            ]
            for row in rows:
                row["payload"] = json.loads(row["payload"])
            return {"events": rows, "next_after": rows[-1]["seq"] if rows else after}
