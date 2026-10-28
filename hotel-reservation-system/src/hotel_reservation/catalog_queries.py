"""Consistent hotel-wide date availability without one request per room type."""

from .common import Error, identifier, integer, stay


def available_rooms(store, hotel, start, end, quantity=1):
    store.owner(hotel)
    integer(quantity, "quantity", 1, 10)
    with store.transaction() as db:
        # The first read pins catalog, inventory and revision to one snapshot.
        revision = db.execute("SELECT revision FROM metadata").fetchone()[0]
        days = stay(start, end, store._now())
        if not db.execute(
            "SELECT 1 FROM hotels WHERE id=? AND active=1", (hotel,)
        ).fetchone():
            raise Error(404, "hotel not available")
        rooms = db.execute(
            "SELECT id,name,description FROM rooms WHERE hotel=? AND active=1 ORDER BY id LIMIT 101",
            (hotel,),
        ).fetchall()
        if len(rooms) > 100:
            raise Error(422, "room-type query budget exceeded")
        output = []
        for room in rooms:
            rows = db.execute(
                "SELECT capacity,overbook,reserved,price FROM inventory WHERE hotel=? AND room=? AND day>=? AND day<? ORDER BY day LIMIT 31",
                (hotel, room["id"], start, end),
            ).fetchall()
            if len(rows) != len(days):
                continue
            available = min(
                row["capacity"]
                + row["capacity"] * row["overbook"] // 100
                - row["reserved"]
                for row in rows
            )
            if available >= quantity:
                unit_total = sum(row["price"] for row in rows)
                output.append(
                    {
                        "id": identifier(room["id"]),
                        "name": room["name"],
                        "description": room["description"],
                        "available": available,
                        "unit_total": unit_total,
                        "total": unit_total * quantity,
                        "currency": "USD",
                    }
                )
        return {
            "hotel": hotel,
            "start": start,
            "end": end,
            "quantity": quantity,
            "room_types": output,
            "revision": revision,
            "advisory": True,
        }
