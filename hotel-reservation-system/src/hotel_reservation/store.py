from datetime import date
import json
import sqlite3
import time
import uuid

from .common import Cache, Database, Error, digest, dumps, identifier, integer, shard_for, stay, text


class Store(Database):
    def __init__(self, path, shard=0, shards=1, hold_seconds=120, clock=time.time):
        super().__init__(path)
        self.shard, self.shards, self.hold_seconds, self.clock = shard, shards, hold_seconds, clock
        integer(shards, "shards", 1, 32)
        integer(shard, "shard", 0, shards-1)
        integer(hold_seconds, "hold_seconds", 1, 900)
        self.cache = Cache()
        self.initialize("""
        CREATE TABLE IF NOT EXISTS metadata(id INTEGER PRIMARY KEY CHECK(id=1),shard INTEGER,shards INTEGER,revision INTEGER);
        CREATE TABLE IF NOT EXISTS hotels(id TEXT PRIMARY KEY,name TEXT,address TEXT,active INTEGER NOT NULL CHECK(active IN(0,1)));
        CREATE TABLE IF NOT EXISTS rooms(hotel TEXT,id TEXT,name TEXT,description TEXT,active INTEGER NOT NULL CHECK(active IN(0,1)),
            PRIMARY KEY(hotel,id),FOREIGN KEY(hotel) REFERENCES hotels(id));
        CREATE TABLE IF NOT EXISTS inventory(hotel TEXT,room TEXT,day TEXT,capacity INTEGER NOT NULL CHECK(capacity>=0),
            overbook INTEGER NOT NULL CHECK(overbook BETWEEN 0 AND 10),price INTEGER NOT NULL CHECK(price>=0),
            reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved>=0 AND reserved<=capacity+(capacity*overbook)/100),
            version INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(hotel,room,day),FOREIGN KEY(hotel,room) REFERENCES rooms(hotel,id));
        CREATE TABLE IF NOT EXISTS bookings(id TEXT PRIMARY KEY,hotel TEXT,room TEXT,user TEXT,request_key TEXT,hash TEXT,
            start TEXT,end TEXT,quantity INTEGER,amount INTEGER,currency TEXT,status TEXT,payment TEXT,
            held INTEGER NOT NULL CHECK(held IN(0,1)),expires REAL,created REAL,method TEXT,
            UNIQUE(hotel,user,request_key),FOREIGN KEY(hotel,room) REFERENCES rooms(hotel,id));
        CREATE INDEX IF NOT EXISTS user_history ON bookings(user,hotel,created);
        CREATE TABLE IF NOT EXISTS nights(booking TEXT,day TEXT,price INTEGER,PRIMARY KEY(booking,day),
            FOREIGN KEY(booking) REFERENCES bookings(id));
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,booking TEXT,action TEXT,status TEXT,
            attempts INTEGER,due REAL,lease REAL,token TEXT,error TEXT,FOREIGN KEY(booking) REFERENCES bookings(id));
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,booking TEXT,kind TEXT,payload TEXT,created REAL);
        """)
        with self.transaction(True) as db:
            db.execute("INSERT OR IGNORE INTO metadata VALUES(1,?,?,0)", (shard, shards))
            meta = db.execute("SELECT * FROM metadata").fetchone()
            if (meta["shard"], meta["shards"]) != (shard, shards):
                raise Error(409, "shard topology differs from stored metadata")

    def owner(self, hotel):
        identifier(hotel)
        if shard_for(hotel, self.shards) != self.shard:
            raise Error(409, "hotel belongs to another shard")

    @staticmethod
    def changed(db):
        db.execute("UPDATE metadata SET revision=revision+1")

    def revision(self):
        with self.transaction() as db:
            return db.execute("SELECT revision FROM metadata").fetchone()[0]

    def catalog(self, hotel, room=None):
        self.owner(hotel)
        if room is not None:
            identifier(room)
        # Revision is persisted: changes made by the independent saga worker
        # invalidate API caches without relying on in-process invalidation.
        key = (self.revision(), "catalog", hotel, room)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        with self.transaction() as db:
            value = db.execute("SELECT * FROM hotels WHERE id=? AND active=1", (hotel,)).fetchone()
            if not value:
                raise Error(404, "hotel not available")
            result = dict(value)
            if room is None:
                result["rooms"] = [dict(r) for r in db.execute("SELECT * FROM rooms WHERE hotel=? AND active=1 ORDER BY id LIMIT 101", (hotel,))]
                if len(result["rooms"]) > 100:
                    raise Error(422, "room-type list exceeds reference limit")
            else:
                value = db.execute("SELECT * FROM rooms WHERE hotel=? AND id=? AND active=1", (hotel, room)).fetchone()
                if not value:
                    raise Error(404, "room type not available")
                result = dict(value)
        self.cache.put(key, result)
        return result

    def manage(self, hotel, body=None, room=None, delete=False):
        self.owner(hotel)
        if room is not None:
            identifier(room)
        with self.transaction(True) as db:
            if delete:
                if room is None:
                    cursor = db.execute("UPDATE hotels SET active=0 WHERE id=?", (hotel,))
                else:
                    cursor = db.execute("UPDATE rooms SET active=0 WHERE hotel=? AND id=?", (hotel, room))
                if not cursor.rowcount:
                    raise Error(404, "catalog entry missing")
            elif room is None:
                if not isinstance(body, dict) or set(body) != {"name", "address"}:
                    raise ValueError("hotel requires name and address")
                db.execute("INSERT INTO hotels VALUES(?,?,?,1) ON CONFLICT(id) DO UPDATE SET name=excluded.name,address=excluded.address,active=1",
                           (hotel, text(body["name"], "name", 200), text(body["address"], "address")))
            else:
                if not isinstance(body, dict) or set(body) != {"name", "description"}:
                    raise ValueError("room type requires name and description")
                if not db.execute("SELECT 1 FROM hotels WHERE id=? AND active=1", (hotel,)).fetchone():
                    raise Error(404, "hotel not available")
                db.execute("INSERT INTO rooms VALUES(?,?,?,?,1) ON CONFLICT(hotel,id) DO UPDATE SET name=excluded.name,description=excluded.description,active=1",
                           (hotel, room, text(body["name"], "name", 200), text(body["description"], "description")))
            self.changed(db)
        return {"updated": True, "active": not delete}

    def set_days(self, hotel, room, rows):
        self.owner(hotel)
        identifier(room)
        if not isinstance(rows, list) or not 1 <= len(rows) <= 730:
            raise ValueError("days must contain 1..730 rows")
        parsed = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"day", "capacity", "overbook", "price"}:
                raise ValueError("day requires day, capacity, overbook and price (USD cents)")
            if date.fromisoformat(row["day"]).isoformat() != row["day"]:
                raise ValueError("noncanonical date")
            parsed.append((hotel, room, row["day"], integer(row["capacity"], "capacity", 0, 100000),
                           integer(row["overbook"], "overbook", 0, 10), integer(row["price"], "price", 0, 100_000_000)))
        if len({r[2] for r in parsed}) != len(parsed):
            raise ValueError("duplicate calendar date")
        with self.transaction(True) as db:
            if not db.execute("SELECT 1 FROM rooms WHERE hotel=? AND id=?", (hotel, room)).fetchone():
                raise Error(404, "room type missing")
            try:
                db.executemany("""INSERT INTO inventory(hotel,room,day,capacity,overbook,price) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(hotel,room,day) DO UPDATE SET capacity=excluded.capacity,overbook=excluded.overbook,
                    price=excluded.price,version=version+1""", parsed)
            except sqlite3.IntegrityError:
                raise Error(409, "new capacity/policy cannot cover existing holds and bookings") from None
            self.changed(db)
        return {"updated_days": len(parsed)}

    def availability(self, hotel, room, start, end):
        self.owner(hotel)
        identifier(room)
        days = stay(start, end, self.clock())
        key = (self.revision(), "availability", hotel, room, start, end)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        self.catalog(hotel, room)
        with self.transaction() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM inventory WHERE hotel=? AND room=? AND day>=? AND day<? ORDER BY day", (hotel, room, start, end))]
        if len(rows) != len(days):
            raise Error(409, "calendar incomplete")
        for row in rows:
            row["limit"] = row["capacity"] + row["capacity"] * row["overbook"] // 100
            row["available"] = row["limit"] - row["reserved"]
        result = {"hotel": hotel, "room": room, "start": start, "end": end, "currency": "USD",
                  "days": rows, "available": min(r["available"] for r in rows),
                  "unit_total": sum(r["price"] for r in rows), "advisory": True}
        self.cache.put(key, result)
        return result

    def emit(self, db, booking, kind, payload=None):
        db.execute("INSERT INTO events(booking,kind,payload,created) VALUES(?,?,?,?)", (booking, kind, dumps(payload or {}), self.clock()))

    def add_job(self, db, booking, action):
        db.execute("INSERT OR IGNORE INTO jobs VALUES(?,?,?,'pending',0,?,0,'','')", (action+":"+booking, booking, action, self.clock()))

    def booking(self, booking, user=None, db=None):
        if db is None:
            with self.transaction() as connection:
                return self.booking(booking, user, connection)
        row = db.execute("SELECT * FROM bookings WHERE id=?", (booking,)).fetchone()
        if not row or (user is not None and row["user"] != user):
            raise Error(404, "reservation missing")
        result = dict(row)
        for key in ("hash", "request_key", "method"):
            result.pop(key)
        result["nights"] = [dict(r) for r in db.execute("SELECT day,price FROM nights WHERE booking=? ORDER BY day", (booking,))]
        return result

    def reserve(self, user, body, before_commit=None):
        fields = {"hotel", "room", "start", "end", "quantity", "request_key", "expected_total", "payment_method"}
        if not isinstance(body, dict) or set(body) != fields:
            raise ValueError("reservation fields must be " + ",".join(sorted(fields)))
        hotel, room, key = identifier(body["hotel"]), identifier(body["room"]), identifier(body["request_key"])
        self.owner(hotel)
        identifier(user)
        quantity = integer(body["quantity"], "quantity", 1, 10)
        expected = integer(body["expected_total"], "expected_total", 0, 30_000_000_000)
        if body["payment_method"] not in ("test-ok", "test-decline"):
            raise ValueError("only simulated test-ok/test-decline payment methods are supported")
        fingerprint = digest(body)
        booking = hotel + "." + digest([user, key])[:32]
        with self.transaction(True) as db:
            old = db.execute("SELECT id,hash FROM bookings WHERE hotel=? AND user=? AND request_key=?", (hotel, user, key)).fetchone()
            if old:
                if old["hash"] != fingerprint:
                    raise Error(409, "idempotency key payload conflict")
                return self.booking(old["id"], user, db)
            days = stay(body["start"], body["end"], self.clock())
            active = db.execute("SELECT 1 FROM rooms r JOIN hotels h ON h.id=r.hotel WHERE r.hotel=? AND r.id=? AND r.active=1 AND h.active=1", (hotel, room)).fetchone()
            if not active:
                raise Error(404, "hotel or room type not available")
            rows = db.execute("SELECT * FROM inventory WHERE hotel=? AND room=? AND day>=? AND day<? ORDER BY day", (hotel, room, body["start"], body["end"])).fetchall()
            if len(rows) != len(days):
                raise Error(409, "calendar incomplete")
            total = quantity * sum(r["price"] for r in rows)
            if total != expected:
                raise Error(409, "price changed; obtain and accept a fresh quote")
            for day in days:
                cursor = db.execute("""UPDATE inventory SET reserved=reserved+?,version=version+1
                    WHERE hotel=? AND room=? AND day=? AND reserved+?<=capacity+(capacity*overbook)/100""",
                                    (quantity, hotel, room, day, quantity))
                if not cursor.rowcount:
                    raise Error(409, "insufficient inventory for at least one night")
            now = self.clock()
            db.execute("INSERT INTO bookings VALUES(?,?,?,?,?,?,?,?,?,?,?,'PENDING','NONE',1,?,?,?)",
                       (booking, hotel, room, user, key, fingerprint, body["start"], body["end"], quantity, total, "USD", now+self.hold_seconds, now, body["payment_method"]))
            db.executemany("INSERT INTO nights VALUES(?,?,?)", [(booking, r["day"], r["price"]) for r in rows])
            self.add_job(db, booking, "charge")
            self.emit(db, booking, "HELD", {"amount": total})
            self.changed(db)
            if before_commit:
                before_commit()
            return self.booking(booking, user, db)

    def release(self, db, row, status):
        if row["held"]:
            db.execute("UPDATE inventory SET reserved=reserved-?,version=version+1 WHERE hotel=? AND room=? AND day>=? AND day<?",
                       (row["quantity"], row["hotel"], row["room"], row["start"], row["end"]))
        db.execute("UPDATE bookings SET status=?,held=0 WHERE id=?", (status, row["id"]))
        self.emit(db, row["id"], status)
        self.changed(db)

    def cancel(self, booking, user=None):
        with self.transaction(True) as db:
            self.booking(booking, user, db)
            row = db.execute("SELECT * FROM bookings WHERE id=?", (booking,)).fetchone()
            if row["status"] in ("PENDING", "CONFIRMED"):
                self.release(db, row, "CANCELLED")
                if row["payment"] == "PAID":
                    db.execute("UPDATE bookings SET payment='REFUND_PENDING' WHERE id=?", (booking,))
                    self.add_job(db, booking, "refund")
            return self.booking(booking, user, db)

    def sweep(self):
        with self.transaction(True) as db:
            rows = db.execute("SELECT * FROM bookings WHERE status='PENDING' AND expires<=? LIMIT 100", (self.clock(),)).fetchall()
            for row in rows:
                self.release(db, row, "EXPIRED")
            return len(rows)

    def claim(self, lease_seconds=2):
        with self.transaction(True) as db:
            now = self.clock()
            row = db.execute("SELECT * FROM jobs WHERE (status='pending' AND due<=?) OR (status='inflight' AND lease<=?) ORDER BY due,id LIMIT 1", (now, now)).fetchone()
            if not row:
                return None
            booking = db.execute("SELECT * FROM bookings WHERE id=?", (row["booking"],)).fetchone()
            if row["action"] == "charge" and row["attempts"] == 0 and booking["status"] != "PENDING":
                db.execute("UPDATE jobs SET status='done' WHERE id=?", (row["id"],))
                self.emit(db, booking["id"], "UNCHARGED")
                return None
            token = uuid.uuid4().hex
            db.execute("UPDATE jobs SET status='inflight',attempts=attempts+1,lease=?,token=? WHERE id=?", (now+lease_seconds, token, row["id"]))
            if row["action"] == "charge":
                db.execute("UPDATE bookings SET payment='CHARGING' WHERE id=? AND payment='NONE'", (booking["id"],))
            return {"id": row["id"], "token": token, "action": row["action"], "booking_id": booking["id"],
                    "amount": booking["amount"], "currency": booking["currency"], "method": booking["method"]}

    def retry(self, job, message):
        with self.transaction(True) as db:
            row = db.execute("SELECT attempts FROM jobs WHERE id=? AND token=? AND status='inflight'", (job["id"], job["token"])).fetchone()
            if row:
                delay = min(30, 0.1 * 2**min(row["attempts"], 8))
                db.execute("UPDATE jobs SET status='pending',due=?,error=? WHERE id=?", (self.clock()+delay, str(message)[:300], job["id"]))

    def finish(self, job, result):
        if result.get("booking_id") != job["booking_id"] or result.get("amount") != job["amount"] or result.get("currency") != "USD":
            raise Error(409, "provider receipt does not match frozen order")
        if result.get("status") not in (("paid", "declined") if job["action"] == "charge" else ("refunded",)):
            raise Error(409, "unexpected provider outcome")
        with self.transaction(True) as db:
            active = db.execute("SELECT * FROM jobs WHERE id=? AND status='inflight' AND token=?", (job["id"], job["token"])).fetchone()
            if not active:
                return False  # Lease fencing: stale worker cannot settle a newer claim.
            row = db.execute("SELECT * FROM bookings WHERE id=?", (active["booking"],)).fetchone()
            if (active["booking"], active["action"], row["amount"], row["currency"]) != (job["booking_id"], job["action"], job["amount"], job["currency"]):
                raise Error(409, "job differs from durable payment intent")
            if job["action"] == "charge":
                if result["status"] == "declined":
                    if row["status"] == "PENDING":
                        self.release(db, row, "REJECTED")
                    db.execute("UPDATE bookings SET payment='DECLINED' WHERE id=?", (row["id"],))
                else:
                    if row["status"] == "PENDING" and row["expires"] > self.clock():
                        db.execute("UPDATE bookings SET status='CONFIRMED',payment='PAID' WHERE id=?", (row["id"],))
                        self.emit(db, row["id"], "CONFIRMED")
                    else:
                        if row["status"] == "PENDING":
                            self.release(db, row, "EXPIRED")
                        db.execute("UPDATE bookings SET payment='REFUND_PENDING' WHERE id=?", (row["id"],))
                        self.add_job(db, row["id"], "refund")
                        self.emit(db, row["id"], "COMPENSATION_REQUIRED")
            else:
                db.execute("UPDATE bookings SET payment='REFUNDED' WHERE id=?", (row["id"],))
                self.emit(db, row["id"], "REFUNDED")
            db.execute("UPDATE jobs SET status='done',error='' WHERE id=?", (job["id"],))
        return True

    def history(self, hotel, user, after=""):
        self.owner(hotel)
        with self.transaction() as db:
            rows = db.execute("SELECT id FROM bookings WHERE hotel=? AND user=? AND id>? ORDER BY id LIMIT 101", (hotel, user, after)).fetchall()
            return {"reservations": [self.booking(r["id"], user, db) for r in rows[:100]],
                    "next_after": rows[99]["id"] if len(rows)>100 else None}

    def status(self):
        with self.transaction() as db:
            return {"shard": self.shard, "shards": self.shards, "revision": db.execute("SELECT revision FROM metadata").fetchone()[0],
                    "bookings": db.execute("SELECT count(*) FROM bookings").fetchone()[0],
                    "unfinished_jobs": db.execute("SELECT count(*) FROM jobs WHERE status!='done'").fetchone()[0],
                    "job_errors": [dict(r) for r in db.execute("SELECT id,attempts,error FROM jobs WHERE status!='done' AND error!='' ORDER BY due LIMIT 20")],
                    "expired_pending": db.execute("SELECT count(*) FROM bookings WHERE status='PENDING' AND expires<=?", (self.clock(),)).fetchone()[0]}

    def events(self, after=0):
        integer(after, "after", 0, 2**53)
        with self.transaction() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT 100", (after,))]
            for row in rows:
                row["payload"] = json.loads(row["payload"])
            return {"events": rows, "next_after": rows[-1]["seq"] if rows else after}
