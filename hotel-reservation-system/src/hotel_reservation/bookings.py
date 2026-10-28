"""Bookings operations for the single hotel-shard owner."""

import json

from .common import Error, digest, dumps, identifier, integer, stay, booking_hotel


class Bookings:
    def booking(self, booking, user=None, db=None):
        booking_hotel(booking)
        if user is not None:
            identifier(user)
        if db is None:
            with self.transaction() as connection:
                return self.booking(booking, user, connection)
        row = db.execute("SELECT * FROM bookings WHERE id=?", (booking,)).fetchone()
        if not row or (user is not None and row["user"] != user):
            raise Error(404, "reservation missing")
        result = dict(row)
        for key in ("hash", "request_key", "method"):
            result.pop(key)
        result["nights"] = [
            dict(r)
            for r in db.execute(
                "SELECT day,price FROM nights WHERE booking=? ORDER BY day", (booking,)
            )
        ]
        return result

    def reserve(self, user, body, before_commit=None):
        fields = {
            "hotel",
            "room",
            "start",
            "end",
            "quantity",
            "request_key",
            "expected_total",
            "payment_method",
        }
        if not isinstance(body, dict) or set(body) != fields:
            raise ValueError("reservation fields must be " + ",".join(sorted(fields)))
        body = json.loads(dumps(body))
        hotel, room, key = (
            identifier(body["hotel"]),
            identifier(body["room"]),
            identifier(body["request_key"]),
        )
        self.owner(hotel)
        identifier(user)
        quantity = integer(body["quantity"], "quantity", 1, 10)
        expected = integer(body["expected_total"], "expected_total", 0, 30_000_000_000)
        if body["payment_method"] not in ("test-ok", "test-decline"):
            raise ValueError(
                "only simulated test-ok/test-decline payment methods are supported"
            )
        fingerprint = digest(body)
        booking = hotel + "." + digest([user, key])[:32]
        with self.transaction(True) as db:
            old = db.execute(
                "SELECT id,hash FROM bookings WHERE hotel=? AND user=? AND request_key=?",
                (hotel, user, key),
            ).fetchone()
            if old:
                if old["hash"] != fingerprint:
                    raise Error(409, "idempotency key payload conflict")
                return self.booking(old["id"], user, db)
            now = self._now()
            days = stay(body["start"], body["end"], now)
            active = db.execute(
                "SELECT 1 FROM rooms r JOIN hotels h ON h.id=r.hotel WHERE r.hotel=? AND r.id=? AND r.active=1 AND h.active=1",
                (hotel, room),
            ).fetchone()
            if not active:
                raise Error(404, "hotel or room type not available")
            rows = db.execute(
                "SELECT * FROM inventory WHERE hotel=? AND room=? AND day>=? AND day<? ORDER BY day",
                (hotel, room, body["start"], body["end"]),
            ).fetchall()
            if len(rows) != len(days):
                raise Error(409, "calendar incomplete")
            total = quantity * sum(r["price"] for r in rows)
            if total != expected:
                raise Error(409, "price changed; obtain and accept a fresh quote")
            for day in days:
                cursor = db.execute(
                    """UPDATE inventory SET reserved=reserved+?,version=version+1
                    WHERE hotel=? AND room=? AND day=? AND reserved+?<=capacity+(capacity*overbook)/100""",
                    (quantity, hotel, room, day, quantity),
                )
                if not cursor.rowcount:
                    raise Error(409, "insufficient inventory for at least one night")
            db.execute(
                "INSERT INTO bookings VALUES(?,?,?,?,?,?,?,?,?,?,?,'PENDING','NONE',1,?,?,?)",
                (
                    booking,
                    hotel,
                    room,
                    user,
                    key,
                    fingerprint,
                    body["start"],
                    body["end"],
                    quantity,
                    total,
                    "USD",
                    now + self.hold_seconds,
                    now,
                    body["payment_method"],
                ),
            )
            db.executemany(
                "INSERT INTO nights VALUES(?,?,?)",
                [(booking, r["day"], r["price"]) for r in rows],
            )
            self.add_job(db, booking, "charge", now=now)
            self.emit(db, booking, "HELD", {"amount": total}, now=now)
            self.changed(db)
            if before_commit:
                before_commit()
            return self.booking(booking, user, db)

    def release(self, db, row, status, *, now=None):
        now = self._now() if now is None else now
        if row["held"]:
            cursor = db.execute(
                "UPDATE inventory SET reserved=reserved-?,version=version+1 WHERE hotel=? AND room=? AND day>=? AND day<? AND reserved>=?",
                (
                    row["quantity"],
                    row["hotel"],
                    row["room"],
                    row["start"],
                    row["end"],
                    row["quantity"],
                ),
            )
            expected = db.execute(
                "SELECT count(*) FROM nights WHERE booking=?", (row["id"],)
            ).fetchone()[0]
            if cursor.rowcount != expected:
                raise Error(503, "held inventory is inconsistent; repair required")
        db.execute(
            "UPDATE bookings SET status=?,held=0 WHERE id=?", (status, row["id"])
        )
        self.emit(db, row["id"], status, now=now)
        self.changed(db)

    def cancel(self, booking, user=None):
        with self.transaction(True) as db:
            self.booking(booking, user, db)
            row = db.execute("SELECT * FROM bookings WHERE id=?", (booking,)).fetchone()
            if row["status"] in ("PENDING", "CONFIRMED"):
                now = self._now()
                self.release(db, row, "CANCELLED", now=now)
                if row["payment"] == "PAID":
                    db.execute(
                        "UPDATE bookings SET payment='REFUND_PENDING' WHERE id=?",
                        (booking,),
                    )
                    self.add_job(db, booking, "refund", now=now)
            return self.booking(booking, user, db)

    def history(self, hotel, user, after=""):
        self.owner(hotel)
        identifier(user)
        if not isinstance(after, str) or (after and booking_hotel(after) != hotel):
            raise ValueError("history cursor must belong to this hotel")
        with self.transaction() as db:
            rows = db.execute(
                "SELECT id FROM bookings WHERE hotel=? AND user=? AND id>? ORDER BY id LIMIT 101",
                (hotel, user, after),
            ).fetchall()
            return {
                "reservations": [self.booking(r["id"], user, db) for r in rows[:100]],
                "next_after": rows[99]["id"] if len(rows) > 100 else None,
            }
