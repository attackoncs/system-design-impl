"""Catalog operations for the single hotel-shard owner."""

from datetime import date
import json
import sqlite3

from .common import Error, dumps, identifier, integer, stay, text


class Catalog:
    def catalog(self, hotel, room=None):
        self.owner(hotel)
        if room is not None:
            identifier(room)
        with self.transaction() as db:
            revision = db.execute("SELECT revision FROM metadata").fetchone()[0]
            key = (revision, "catalog", hotel, room)
            return self.cache.load(key, lambda: self._catalog(db, hotel, room))

    @staticmethod
    def _catalog(db, hotel, room=None):
        value = db.execute(
            "SELECT * FROM hotels WHERE id=? AND active=1", (hotel,)
        ).fetchone()
        if not value:
            raise Error(404, "hotel not available")
        if room is not None:
            value = db.execute(
                "SELECT * FROM rooms WHERE hotel=? AND id=? AND active=1", (hotel, room)
            ).fetchone()
            if not value:
                raise Error(404, "room type not available")
            return dict(value)
        result = dict(value)
        result["rooms"] = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM rooms WHERE hotel=? AND active=1 ORDER BY id LIMIT 101",
                (hotel,),
            )
        ]
        if len(result["rooms"]) > 100:
            raise Error(422, "room-type list exceeds reference limit")
        return result

    def manage(self, hotel, body=None, room=None, delete=False):
        self.owner(hotel)
        if type(delete) is not bool:
            raise ValueError("delete must be boolean")
        body = json.loads(dumps(body))
        if room is not None:
            identifier(room)
        with self.transaction(True) as db:
            if delete:
                if room is None:
                    cursor = db.execute(
                        "UPDATE hotels SET active=0 WHERE id=?", (hotel,)
                    )
                else:
                    cursor = db.execute(
                        "UPDATE rooms SET active=0 WHERE hotel=? AND id=?",
                        (hotel, room),
                    )
                if not cursor.rowcount:
                    raise Error(404, "catalog entry missing")
            elif room is None:
                if not isinstance(body, dict) or set(body) != {"name", "address"}:
                    raise ValueError("hotel requires name and address")
                db.execute(
                    "INSERT INTO hotels VALUES(?,?,?,1) ON CONFLICT(id) DO UPDATE SET name=excluded.name,address=excluded.address,active=1",
                    (
                        hotel,
                        text(body["name"], "name", 200),
                        text(body["address"], "address"),
                    ),
                )
            else:
                if not isinstance(body, dict) or set(body) != {"name", "description"}:
                    raise ValueError("room type requires name and description")
                if not db.execute(
                    "SELECT 1 FROM hotels WHERE id=? AND active=1", (hotel,)
                ).fetchone():
                    raise Error(404, "hotel not available")
                db.execute(
                    "INSERT INTO rooms VALUES(?,?,?,?,1) ON CONFLICT(hotel,id) DO UPDATE SET name=excluded.name,description=excluded.description,active=1",
                    (
                        hotel,
                        room,
                        text(body["name"], "name", 200),
                        text(body["description"], "description"),
                    ),
                )
            self.changed(db)
        return {"updated": True, "active": not delete}

    def set_days(self, hotel, room, rows):
        self.owner(hotel)
        identifier(room)
        if not isinstance(rows, list) or not 1 <= len(rows) <= 730:
            raise ValueError("days must contain 1..730 rows")
        rows = json.loads(dumps(rows))
        parsed = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {
                "day",
                "capacity",
                "overbook",
                "price",
            }:
                raise ValueError(
                    "day requires day, capacity, overbook and price (USD cents)"
                )
            if (
                not isinstance(row["day"], str)
                or date.fromisoformat(row["day"]).isoformat() != row["day"]
            ):
                raise ValueError("noncanonical date")
            parsed.append(
                (
                    hotel,
                    room,
                    row["day"],
                    integer(row["capacity"], "capacity", 0, 100000),
                    integer(row["overbook"], "overbook", 0, 10),
                    integer(row["price"], "price", 0, 100_000_000),
                )
            )
        if len({r[2] for r in parsed}) != len(parsed):
            raise ValueError("duplicate calendar date")
        with self.transaction(True) as db:
            if not db.execute(
                "SELECT 1 FROM rooms WHERE hotel=? AND id=?", (hotel, room)
            ).fetchone():
                raise Error(404, "room type missing")
            try:
                db.executemany(
                    """INSERT INTO inventory(hotel,room,day,capacity,overbook,price) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(hotel,room,day) DO UPDATE SET capacity=excluded.capacity,overbook=excluded.overbook,
                    price=excluded.price,version=version+1""",
                    parsed,
                )
            except sqlite3.IntegrityError:
                raise Error(
                    409, "new capacity/policy cannot cover existing holds and bookings"
                ) from None
            self.changed(db)
        return {"updated_days": len(parsed)}

    def availability(self, hotel, room, start, end):
        self.owner(hotel)
        identifier(room)
        with self.transaction() as db:
            revision = db.execute("SELECT revision FROM metadata").fetchone()[0]
            days = stay(start, end, self._now())
            key = (revision, "availability", hotel, room, start, end)

            def load():
                self._catalog(db, hotel, room)
                rows = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM inventory WHERE hotel=? AND room=? AND day>=? AND day<? ORDER BY day",
                        (hotel, room, start, end),
                    )
                ]
                if len(rows) != len(days):
                    raise Error(409, "calendar incomplete")
                for row in rows:
                    row["limit"] = (
                        row["capacity"] + row["capacity"] * row["overbook"] // 100
                    )
                    row["available"] = row["limit"] - row["reserved"]
                return {
                    "hotel": hotel,
                    "room": room,
                    "start": start,
                    "end": end,
                    "currency": "USD",
                    "days": rows,
                    "available": min(row["available"] for row in rows),
                    "unit_total": sum(row["price"] for row in rows),
                    "advisory": True,
                }

            return self.cache.load(key, load)
