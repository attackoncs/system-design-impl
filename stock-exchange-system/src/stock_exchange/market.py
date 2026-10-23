import json

from .common import Database, Error, encode, integer, request
from .matching import book


def candles(trades, resolution=60):
    integer(resolution, 1, 86400)
    rows = {}
    for trade in trades:
        start = trade["timestamp"] // resolution * resolution
        key = (trade["symbol"], start)
        if key not in rows:
            rows[key] = {
                "symbol": trade["symbol"],
                "start": start,
                "open": trade["price"],
                "high": trade["price"],
                "low": trade["price"],
                "close": trade["price"],
                "volume": 0,
            }
        candle = rows[key]
        candle["high"] = max(candle["high"], trade["price"])
        candle["low"] = min(candle["low"], trade["price"])
        candle["close"] = trade["price"]
        candle["volume"] += trade["quantity"]
    return list(rows.values())


class Market(Database):
    def __init__(self, path):
        super().__init__(path)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS projection(id INTEGER PRIMARY KEY CHECK(id=1),value TEXT,resyncs INTEGER);
        CREATE TABLE IF NOT EXISTS changes(seq INTEGER PRIMARY KEY,body TEXT);
        """)

    def view(self):
        with self.connection() as db:
            row = db.execute("SELECT * FROM projection").fetchone()
            if not row:
                raise Error(503, "market projection initializing")
            return {**json.loads(row["value"]), "resyncs": row["resyncs"]}

    def replace(self, snapshot):
        state = snapshot["state"]
        value = {
            "seq": snapshot["seq"],
            "head": snapshot["head"],
            "books": {
                symbol: book(state, symbol) for symbol in state["settings"]["symbols"]
            },
            "orders": state["orders"],
            "trades": state["trades"],
        }
        with self.transaction() as db:
            old = db.execute("SELECT resyncs FROM projection").fetchone()
            db.execute(
                "INSERT OR REPLACE INTO projection VALUES(1,?,?)",
                (encode(value), old[0] + 1 if old else 1),
            )
            db.execute("DELETE FROM changes")

    def apply(self, batch):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM projection").fetchone()
            value = json.loads(row["value"])
            if batch["seq"] <= value["seq"]:
                if batch["seq"] == value["seq"] and batch["head"] != value["head"]:
                    raise Error(409, "market duplicate content conflict")
                return
            if batch["seq"] != value["seq"] + 1 or batch["previous"] != value["head"]:
                raise Error(410, "market sequence gap")
            for symbol, sides in batch["books"].items():
                for side, changes in sides.items():
                    for price, quantity in changes.items():
                        if quantity:
                            value["books"][symbol][side][price] = quantity
                        else:
                            value["books"][symbol][side].pop(price, None)
            value["orders"].update(
                {order["order_id"]: order for order in batch["orders"]}
            )
            value["trades"].extend(batch["trades"])
            value.update(seq=batch["seq"], head=batch["head"])
            db.execute("UPDATE projection SET value=? WHERE id=1", (encode(value),))
            public = {key: batch[key] for key in ("seq", "previous", "head", "books")}
            public["trades"] = [
                {
                    key: trade[key]
                    for key in ("id", "seq", "symbol", "price", "quantity", "timestamp")
                }
                for trade in batch["trades"]
            ]
            db.execute(
                "INSERT INTO changes VALUES(?,?)", (batch["seq"], encode(public))
            )
            db.execute("DELETE FROM changes WHERE seq<=?", (batch["seq"] - 8,))

    def deltas(self, after):
        integer(after)
        with self.transaction() as db:
            row = db.execute("SELECT value FROM projection").fetchone()
            if not row:
                raise Error(503, "market projection initializing")
            value = json.loads(row[0])
            if after > value["seq"]:
                raise Error(409, "client sequence ahead of publisher")
            oldest = db.execute("SELECT min(seq) FROM changes").fetchone()[0]
            if after < value["seq"] and (oldest is None or after < oldest - 1):
                raise Error(410, "public delta gap; fetch /market/snapshot")
            return {
                "seq": value["seq"],
                "deltas": [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT body FROM changes WHERE seq>? ORDER BY seq", (after,)
                    )
                ],
            }

    def poll(self, config):
        token = config["internal_token"]
        control = request(config["gateway"] + "/internal/control", token)
        url = config["engines"][control["active"]]
        try:
            view = self.view()
        except Error:
            self.replace(
                request(url + "/internal/serve?epoch=" + str(control["epoch"]), token)
            )
            return
        try:
            feed = request(
                url
                + "/internal/feed?after="
                + str(view["seq"])
                + "&epoch="
                + str(control["epoch"]),
                token,
            )
            for batch in feed["batches"]:
                self.apply(batch)
        except Error as exc:
            if exc.status != 410:
                raise
            self.replace(
                request(url + "/internal/serve?epoch=" + str(control["epoch"]), token)
            )


def levels(value, symbol, depth):
    if symbol not in value["books"]:
        raise Error(404, "symbol missing")
    book_value = value["books"][symbol]
    return {
        "seq": value["seq"],
        "symbol": symbol,
        **{
            side: [
                {"price": int(price), "quantity": quantity}
                for price, quantity in sorted(
                    book_value[side].items(),
                    key=lambda item: int(item[0]),
                    reverse=side == "bids",
                )[:depth]
            ]
            for side in ("bids", "asks")
        },
    }


def report(value, owner):
    fills = []
    for trade in value["trades"]:
        for side, key, order in (
            ("BUY", "buyer", "buy_order"),
            ("SELL", "seller", "sell_order"),
        ):
            if trade[key] == owner:
                fills.append(
                    {
                        "id": trade["id"] + "_" + side,
                        "trade_id": trade["id"],
                        "order_id": trade[order],
                        "symbol": trade["symbol"],
                        "side": side,
                        "price": trade["price"],
                        "quantity": trade["quantity"],
                        "seq": trade["seq"],
                    }
                )
    return {
        "seq": value["seq"],
        "orders": [row for row in value["orders"].values() if row["owner"] == owner],
        "fills": fills,
    }
