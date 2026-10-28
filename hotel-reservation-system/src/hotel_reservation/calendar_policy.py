"""Bounded rolling calendar fill; existing prices and held inventory are immutable.

Policies provide defaults for missing dates only. Updating a policy is never a
bulk repricing operation. One persistent cursor per policy makes restart safe.
"""

from datetime import datetime, timedelta, timezone
import json
import uuid

from .common import Error, dumps, identifier, integer


def _today(now):
    return datetime.fromtimestamp(now, timezone.utc).date()


class CalendarPolicies:
    def __init__(self, store):
        self.store = store
        with store.transaction(True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS calendar_policies(
                hotel TEXT,room TEXT,capacity INTEGER NOT NULL,overbook INTEGER NOT NULL,
                price INTEGER NOT NULL,horizon_days INTEGER NOT NULL,version INTEGER NOT NULL,
                next_day TEXT NOT NULL,served REAL NOT NULL,updated REAL NOT NULL,
                PRIMARY KEY(hotel,room),FOREIGN KEY(hotel,room) REFERENCES rooms(hotel,id))""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS policy_schedule ON calendar_policies(served,hotel,room)"
            )
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(calendar_policies)")
            }
            if "generation" not in columns:
                db.execute(
                    "ALTER TABLE calendar_policies ADD COLUMN generation TEXT NOT NULL DEFAULT ''"
                )
                db.execute(
                    "UPDATE calendar_policies SET generation=lower(hex(randomblob(16))) WHERE generation=''"
                )

    def _key(self, hotel, room):
        self.store.owner(hotel)
        identifier(room)

    @staticmethod
    def _active(db, hotel, room):
        return db.execute(
            "SELECT 1 FROM rooms r JOIN hotels h ON h.id=r.hotel WHERE r.hotel=? AND r.id=? AND r.active=1 AND h.active=1",
            (hotel, room),
        ).fetchone()

    def get(self, hotel, room):
        self._key(hotel, room)
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM calendar_policies WHERE hotel=? AND room=?",
                (hotel, room),
            ).fetchone()
            if row is None:
                raise Error(404, "calendar policy missing")
            return dict(row)

    def put(self, hotel, room, body):
        self._key(hotel, room)
        if (
            not isinstance(body, dict)
            or not {"capacity", "overbook", "price"} <= set(body)
            or set(body) - {"capacity", "overbook", "price", "horizon_days"}
        ):
            raise ValueError(
                "policy requires capacity, overbook and price; optional horizon_days"
            )
        # Own and validate scalar values before a writer wait.
        body = json.loads(dumps(body))
        capacity = integer(body["capacity"], "capacity", 0, 100000)
        overbook = integer(body["overbook"], "overbook", 0, 10)
        price = integer(body["price"], "price", 0, 100000000)
        horizon = integer(body.get("horizon_days", 730), "horizon_days", 1, 730)
        with self.store.transaction(True) as db:
            now = self.store._now()
            today = _today(now).isoformat()
            if not self._active(db, hotel, room):
                raise Error(404, "hotel or room type not available")
            exists = db.execute(
                "SELECT * FROM calendar_policies WHERE hotel=? AND room=?",
                (hotel, room),
            ).fetchone()
            if exists and tuple(
                exists[key] for key in ("capacity", "overbook", "price", "horizon_days")
            ) == (capacity, overbook, price, horizon):
                return dict(exists)
            if (
                not exists
                and db.execute("SELECT count(*) FROM calendar_policies").fetchone()[0]
                >= 10000
            ):
                raise Error(429, "calendar policy capacity reached")
            db.execute(
                """INSERT INTO calendar_policies(hotel,room,capacity,overbook,price,horizon_days,version,next_day,served,updated,generation)
                VALUES(?,?,?,?,?,?,1,?,0,?,?)
                ON CONFLICT(hotel,room) DO UPDATE SET capacity=excluded.capacity,overbook=excluded.overbook,
                price=excluded.price,horizon_days=excluded.horizon_days,version=version+1,
                next_day=excluded.next_day,served=0,updated=excluded.updated,generation=excluded.generation""",
                (
                    hotel,
                    room,
                    capacity,
                    overbook,
                    price,
                    horizon,
                    today,
                    now,
                    uuid.uuid4().hex,
                ),
            )
            return dict(
                db.execute(
                    "SELECT * FROM calendar_policies WHERE hotel=? AND room=?",
                    (hotel, room),
                ).fetchone()
            )

    def delete(self, hotel, room):
        self._key(hotel, room)
        with self.store.transaction(True) as db:
            db.execute(
                "DELETE FROM calendar_policies WHERE hotel=? AND room=?", (hotel, room)
            )
        return {"disabled": True, "existing_inventory_preserved": True}

    def _due(self, db, today, limit):
        return db.execute(
            """SELECT p.* FROM calendar_policies p JOIN hotels h ON h.id=p.hotel
            JOIN rooms r ON r.hotel=p.hotel AND r.id=p.room
            WHERE h.active=1 AND r.active=1 AND p.next_day < date(?, '+' || p.horizon_days || ' days')
            ORDER BY p.served,p.hotel,p.room LIMIT ?""",
            (today, limit),
        ).fetchall()

    def roll_once(self, max_policies=32, max_rows=4096):
        integer(max_policies, "max_policies", 1, 128)
        integer(max_rows, "max_rows", 1, 8192)
        observed_day = _today(self.store._now())
        with self.store.transaction() as db:
            policies = [
                dict(row)
                for row in self._due(db, observed_day.isoformat(), max_policies)
            ]
        inserted = examined = processed = 0
        for policy in policies:
            if examined >= max_rows:
                break
            self._key(policy["hotel"], policy["room"])
            begin = max(observed_day, datetime.fromisoformat(policy["next_day"]).date())
            end = observed_day + timedelta(days=policy["horizon_days"])
            count = min((end - begin).days, max_rows - examined)
            # Date construction and row capture stay outside the inventory writer.
            rows = [
                (
                    policy["hotel"],
                    policy["room"],
                    (begin + timedelta(days=offset)).isoformat(),
                    policy["capacity"],
                    policy["overbook"],
                    policy["price"],
                )
                for offset in range(max(0, count))
            ]
            if not rows:
                continue
            with self.store.transaction(True) as db:
                now = self.store._now()
                if _today(now) != observed_day:
                    break  # Retry against a new UTC day instead of mixing horizons.
                current = db.execute(
                    "SELECT version,next_day,generation FROM calendar_policies WHERE hotel=? AND room=?",
                    (policy["hotel"], policy["room"]),
                ).fetchone()
                if (
                    current is None
                    or (current["version"], current["next_day"], current["generation"])
                    != (policy["version"], policy["next_day"], policy["generation"])
                    or not self._active(db, policy["hotel"], policy["room"])
                ):
                    continue
                cursor = db.executemany(
                    "INSERT OR IGNORE INTO inventory(hotel,room,day,capacity,overbook,price) VALUES(?,?,?,?,?,?)",
                    rows,
                )
                added = cursor.rowcount
                db.execute(
                    "UPDATE calendar_policies SET next_day=?,served=? WHERE hotel=? AND room=? AND version=?",
                    (
                        (begin + timedelta(days=count)).isoformat(),
                        now,
                        policy["hotel"],
                        policy["room"],
                        policy["version"],
                    ),
                )
                if added:
                    self.store.changed(db)
                inserted += added
            examined += len(rows)
            processed += 1
        with self.store.transaction() as db:
            pending = bool(self._due(db, observed_day.isoformat(), 1))
        return {
            "as_of": observed_day.isoformat(),
            "policies_processed": processed,
            "examined_days": examined,
            "inserted_days": inserted,
            "pending": pending,
        }
