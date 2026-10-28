import json

from .common import Database, Error, checksum, encode, integer, request
from .contracts import freeze, fields
from .publication import validate_snapshot, validate_batch, validate_projection
import threading
import time
from .matching import book


from .queries import candles as candles, levels as levels, report as report


class Market(Database):
    def __init__(self, path):
        super().__init__(path)
        self.poll_lock = threading.Lock()
        self.initialize("""
        CREATE TABLE IF NOT EXISTS projection(id INTEGER PRIMARY KEY CHECK(id=1),value TEXT,resyncs INTEGER);
        CREATE TABLE IF NOT EXISTS changes(seq INTEGER PRIMARY KEY,body TEXT);
        CREATE TABLE IF NOT EXISTS receipts(seq INTEGER PRIMARY KEY,hash TEXT NOT NULL);
        """)

    def view(self):
        with self.connection() as db:
            row = db.execute("SELECT * FROM projection").fetchone()
            if not row:
                raise Error(503, "market projection initializing")
            value = json.loads(row["value"])
            if "source_hash" not in value:
                raise Error(503, "legacy market projection requires source resnapshot")
            return {**value, "resyncs": row["resyncs"]}

    def replace(self, snapshot, epoch=0):
        snapshot = validate_snapshot(snapshot)
        integer(epoch)
        state = snapshot["state"]
        value = {
            "seq": snapshot["seq"],
            "head": snapshot["head"],
            "source_hash": snapshot["state_hash"],
            "epoch": epoch,
            "settings": state["settings"],
            "books": {
                symbol: book(state, symbol) for symbol in state["settings"]["symbols"]
            },
            "orders": state["orders"],
            "trades": state["trades"],
        }
        with self.transaction() as db:
            old = db.execute("SELECT * FROM projection").fetchone()
            if old:
                previous = json.loads(old["value"])
                if epoch < previous.get("epoch", 0) or value["seq"] < previous["seq"]:
                    raise Error(409, "market snapshot/epoch cannot regress")
                if value["seq"] == previous["seq"]:
                    comparison = {
                        key: previous[key] for key in ("books", "orders", "trades")
                    }
                    candidate = {key: value[key] for key in comparison}
                    if "source_hash" not in previous:
                        candidate["trades"] = [
                            {
                                key: item
                                for key, item in row.items()
                                if key != "execution_seq"
                            }
                            for row in candidate["trades"]
                        ]
                    if value["head"] != previous["head"] or comparison != candidate:
                        raise Error(409, "conflicting market snapshot")
                    if (
                        "source_hash" in previous
                        and value["source_hash"] != previous["source_hash"]
                    ):
                        raise Error(409, "same-head snapshot state changed")
                    if value == previous:
                        return
            db.execute(
                "INSERT OR REPLACE INTO projection VALUES(1,?,?)",
                (encode(value), old["resyncs"] + 1 if old else 1),
            )
            db.execute("DELETE FROM changes")
            db.execute("DELETE FROM receipts")

    def apply(self, batch, epoch=0):
        batch = validate_batch(batch)
        integer(epoch)
        fingerprint = checksum(batch)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM projection").fetchone()
            if not row:
                raise Error(503, "market projection initializing")
            value = json.loads(row["value"])
            if epoch < value.get("epoch", 0):
                raise Error(409, "market publisher epoch regressed")
            if batch["seq"] <= value["seq"]:
                receipt = db.execute(
                    "SELECT hash FROM receipts WHERE seq=?", (batch["seq"],)
                ).fetchone()
                if receipt is None:
                    raise Error(
                        410, "old batch outside verified receipt window; resnapshot"
                    )
                if receipt[0] != fingerprint:
                    raise Error(409, "market duplicate content conflict")
                return
            if batch["seq"] != value["seq"] + 1 or batch["previous"] != value["head"]:
                raise Error(410, "market sequence gap")
            if any(symbol not in value["books"] for symbol in batch["books"]):
                raise ValueError("unknown market symbol")
            existing_trades = {trade["id"] for trade in value["trades"]}
            previous_execution = (
                value["trades"][-1]["execution_seq"] if value["trades"] else 0
            )
            for trade in batch["trades"]:
                if (
                    trade["id"] in existing_trades
                    or trade["execution_seq"] != previous_execution + 1
                    or trade["seq"] != batch["seq"]
                    or trade["symbol"] not in value["books"]
                ):
                    raise ValueError(
                        "duplicate/gapped execution or wrong market symbol"
                    )
                existing_trades.add(trade["id"])
                previous_execution += 1
            for order in batch["orders"]:
                if order["symbol"] not in value["books"]:
                    raise ValueError("unknown order symbol")
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
            validate_projection(value)
            value.update(
                seq=batch["seq"],
                head=batch["head"],
                source_hash=batch["state_hash"],
                epoch=epoch,
            )
            db.execute("UPDATE projection SET value=? WHERE id=1", (encode(value),))
            public = {key: batch[key] for key in ("seq", "previous", "head", "books")}
            public["trades"] = [
                {
                    key: trade[key]
                    for key in (
                        "id",
                        "seq",
                        "execution_seq",
                        "symbol",
                        "price",
                        "quantity",
                        "timestamp",
                    )
                }
                for trade in batch["trades"]
            ]
            db.execute(
                "INSERT INTO changes VALUES(?,?)", (batch["seq"], encode(public))
            )
            db.execute("INSERT INTO receipts VALUES(?,?)", (batch["seq"], fingerprint))
            db.execute("DELETE FROM receipts WHERE seq<=?", (batch["seq"] - 8,))
            db.execute("DELETE FROM changes WHERE seq<=?", (batch["seq"] - 8,))

    def deltas(self, after):
        integer(after)
        with self.read_transaction() as db:
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

    def poll(self, config, *, deadline=None, cancel=None):
        config = freeze(config)
        deadline = time.monotonic() + 5 if deadline is None else deadline
        # Network waits never own a reader's database transaction or HTTP lock.
        with self.poll_lock:
            token = config["internal_token"]

            def rpc(url):
                return request(url, token, timeout=2, deadline=deadline, cancel=cancel)

            control = rpc(config["gateway"] + "/internal/control")
            url = config["engines"][control["active"]]
            epoch = integer(control["epoch"], 1)
            try:
                view = self.view()
            except Error:
                view = None
            snapshot, feed = None, None
            if view is None:
                snapshot = rpc(url + "/internal/serve?epoch=" + str(epoch))
            else:
                try:
                    feed = rpc(
                        url
                        + "/internal/feed?after="
                        + str(view["seq"])
                        + "&epoch="
                        + str(epoch)
                    )
                except Error as exc:
                    if exc.status != 410:
                        raise
                    snapshot = rpc(url + "/internal/serve?epoch=" + str(epoch))
            current = rpc(config["gateway"] + "/internal/control")
            if (current["epoch"], current["active"]) != (epoch, control["active"]):
                raise Error(409, "publisher changed during market poll")
            checkpoint = (
                snapshot
                if snapshot is not None
                else (feed["batches"][-1] if feed.get("batches") else view)
            )
            sequence, head = (
                integer(checkpoint["seq"], 1, current["seq"]),
                checkpoint.get("head"),
            )
            prefix = rpc(
                config["gateway"] + "/internal/events?after=" + str(sequence - 1)
            )
            if not prefix.get("events") or prefix["events"][0]["hash"] != head:
                raise ValueError("market checkpoint is outside sequencer history")
            if snapshot is not None:
                self.replace(snapshot, epoch)
            else:
                fields(feed, {"batches"})
                if not isinstance(feed["batches"], list) or len(feed["batches"]) > 1000:
                    raise ValueError("bounded batch list required")
                batches = [validate_batch(batch) for batch in feed["batches"]]
                for batch in batches:
                    self.apply(batch, epoch)

    @staticmethod
    def executions(value, owner, query):
        symbol = query.get("symbol", "")
        if symbol not in value["books"]:
            raise Error(404, "symbol missing")
        result = []
        for fill in report(value, owner)["fills"]:
            if (
                fill["symbol"] == symbol
                and ("orderId" not in query or query["orderId"] == fill["order_id"])
                and query.get("startTime", 0)
                <= fill["timestamp"]
                < query.get("endTime", 253402300799)
            ):
                result.append({**fill, "order_type": "LIMIT"})
        return result
