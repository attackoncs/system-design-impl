"""Validate snapshots/deltas before they can change a market cursor or report."""

from .common import checksum, identifier, integer
from .contracts import MAX_TIMESTAMP, fields, freeze, hash_value, settings, validate
from .matching import audit


def validate_projection(value):
    """Independently reconcile public levels with private order/fill facts."""
    from .order_book import OrderBook

    books = OrderBook(OrderBook.empty(value["settings"]["symbols"]))
    filled = {id: 0 for id in value["orders"]}
    for id, item in value["orders"].items():
        if item["owner"] not in value["settings"]["accounts"] or id != item["order_id"]:
            raise ValueError("projection order identity/owner mismatch")
        if item["status"] in {"OPEN", "PARTIAL"}:
            books.add(item)
    if any(books.levels(symbol) != levels for symbol, levels in value["books"].items()):
        raise ValueError("book delta differs from open orders")
    for item in value["trades"]:
        bid, ask = (
            value["orders"][item["buy_order"]],
            value["orders"][item["sell_order"]],
        )
        if (
            bid["owner"] != item["buyer"]
            or ask["owner"] != item["seller"]
            or bid["side"] != "BUY"
            or ask["side"] != "SELL"
            or bid["symbol"] != item["symbol"]
            or ask["symbol"] != item["symbol"]
            or not ask["price"] <= item["price"] <= bid["price"]
        ):
            raise ValueError("execution references changed parties/limits")
        filled[item["buy_order"]] += item["quantity"]
        filled[item["sell_order"]] += item["quantity"]
    if any(item["filled"] != filled[id] for id, item in value["orders"].items()):
        raise ValueError("order/execution quantity mismatch")


def order(value):
    fields(
        value,
        {
            "request_id",
            "order_id",
            "symbol",
            "side",
            "price",
            "quantity",
            "owner",
            "seq",
            "timestamp",
            "day",
            "remaining",
            "filled",
            "status",
            "reason",
        },
    )
    validate(
        {
            key: value[key]
            for key in ("request_id", "order_id", "symbol", "side", "price", "quantity")
        },
        "new",
    )
    identifier(value["owner"])
    integer(value["seq"], 2, 1001)
    integer(value["timestamp"], 1, MAX_TIMESTAMP)
    integer(value["remaining"], 0, value["quantity"])
    integer(value["filled"], 0, value["quantity"])
    if value["remaining"] + value["filled"] != value["quantity"] or value[
        "status"
    ] not in {"OPEN", "PARTIAL", "FILLED", "CANCELED"}:
        raise ValueError("invalid order quantities/status")
    if (
        (value["status"] == "FILLED") != (value["remaining"] == 0)
        or value["status"] == "OPEN"
        and value["filled"] != 0
        or value["status"] == "PARTIAL"
        and not 0 < value["filled"] < value["quantity"]
    ):
        raise ValueError("inconsistent order status")
    if not isinstance(value["day"], str) or not isinstance(value["reason"], str):
        raise ValueError("order day/reason required")


def trade(value):
    fields(
        value,
        {
            "id",
            "seq",
            "execution_seq",
            "symbol",
            "price",
            "quantity",
            "timestamp",
            "buy_order",
            "sell_order",
            "buyer",
            "seller",
        },
    )
    for key in ("id", "symbol", "buy_order", "sell_order", "buyer", "seller"):
        identifier(value[key])
    if value["buyer"] == value["seller"]:
        raise ValueError("self trade")
    integer(value["seq"], 2, 1001)
    integer(value["execution_seq"], 1, 1_000_000)
    integer(value["price"], 1, 100_000_000)
    integer(value["quantity"], 1, 1_000_000)
    integer(value["timestamp"], 1, MAX_TIMESTAMP)


def validate_snapshot(snapshot):
    snapshot = freeze(snapshot)
    fields(snapshot, {"seq", "head", "state", "state_hash"})
    integer(snapshot["seq"], 1, 1001)
    hash_value(snapshot["head"])
    if checksum(snapshot["state"]) != hash_value(snapshot["state_hash"]):
        raise ValueError("snapshot state checksum mismatch")
    state = snapshot["state"]
    fields(
        state,
        {
            "accounts",
            "orders",
            "trades",
            "results",
            "daily",
            "settings",
            "book_index",
            "execution_seq",
            "last_seq",
            "last_timestamp",
        },
    )
    configuration = settings(state["settings"])
    if set(state["accounts"]) != set(configuration["accounts"]):
        raise ValueError("snapshot accounts changed")
    for owner, account in state["accounts"].items():
        fields(account, {"cash", "positions"})
        if set(account["positions"]) != set(configuration["symbols"]):
            raise ValueError("snapshot positions changed")
        for balance in [account["cash"], *account["positions"].values()]:
            fields(balance, {"available", "held"})
            for value in balance.values():
                integer(value, 0, 10**16)
    for key, value in state["orders"].items():
        order(value)
        if key != value["order_id"]:
            raise ValueError("order map identity mismatch")
    for value in state["trades"]:
        trade(value)
    if state["last_seq"] != snapshot["seq"] or not audit(state)["matches"]:
        raise ValueError("snapshot conservation/index/sequence mismatch")
    return snapshot


def validate_batch(batch):
    batch = freeze(batch)
    fields(
        batch, {"seq", "previous", "head", "state_hash", "books", "orders", "trades"}
    )
    integer(batch["seq"], 1, 1001)
    for key in ("previous", "head", "state_hash"):
        hash_value(batch[key])
    if (
        not isinstance(batch["books"], dict)
        or not isinstance(batch["orders"], list)
        or not isinstance(batch["trades"], list)
    ):
        raise ValueError("batch maps/lists required")
    if len(batch["orders"]) > 1000 or len(batch["trades"]) > 1000:
        raise ValueError("batch exceeds command budget")
    for symbol, sides in batch["books"].items():
        identifier(symbol)
        fields(sides, {"bids", "asks"})
        for changes in sides.values():
            if not isinstance(changes, dict):
                raise ValueError("price map required")
            for price, quantity in changes.items():
                if (
                    not isinstance(price, str)
                    or not price.isascii()
                    or not price.isdecimal()
                    or str(int(price)) != price
                ):
                    raise ValueError("canonical positive integer price required")
                integer(int(price), 1, 100_000_000)
                integer(quantity, 0, 10**9)
    ids = set()
    for value in batch["orders"]:
        order(value)
        if value["order_id"] in ids:
            raise ValueError("duplicate order update")
        ids.add(value["order_id"])
    for value in batch["trades"]:
        trade(value)
    return batch
