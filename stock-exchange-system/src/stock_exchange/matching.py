import copy
from datetime import datetime, timezone

from .common import identifier, integer


from .contracts import fields, settings as validate_settings, validate as validate
from .contracts import MAX_TIMESTAMP
from .order_book import OrderBook
from .policies import (
    SessionPolicy as SessionPolicy,
    UTCSession as UTCSession,
    RiskPolicy as RiskPolicy,
    SubmittedVolumeRisk as SubmittedVolumeRisk,
)


def book(state, symbol):
    if "book_index" in state:
        return OrderBook(state["book_index"]).levels(symbol)
    levels = {"bids": {}, "asks": {}}
    for order in state["orders"].values():
        if order["symbol"] == symbol and order["status"] in ("OPEN", "PARTIAL"):
            side = "bids" if order["side"] == "BUY" else "asks"
            price = str(order["price"])
            levels[side][price] = levels[side].get(price, 0) + order["remaining"]
    return levels


def release(state, order, reason):
    OrderBook(state["book_index"]).remove(order)
    account = state["accounts"][order["owner"]]
    remaining = order["remaining"]
    if order["side"] == "BUY":
        amount = remaining * order["price"]
        account["cash"]["held"] -= amount
        account["cash"]["available"] += amount
    else:
        position = account["positions"][order["symbol"]]
        position["held"] -= remaining
        position["available"] += remaining
    order["status"] = "CANCELED"
    order["reason"] = reason


def reduce_command(state, command):
    """Pure sequenced matching, replaying commands never emits network orders."""
    if not isinstance(command, dict):
        raise ValueError("command object required")
    if command.get("kind") == "genesis":
        fields(command, {"kind", "settings"})
        if state:
            raise ValueError("duplicate genesis")
        data = validate_settings(command["settings"])
        accounts = {
            owner: {
                "cash": {"available": value["cash"], "held": 0},
                "positions": {
                    symbol: {"available": value["positions"].get(symbol, 0), "held": 0}
                    for symbol in data["symbols"]
                },
            }
            for owner, value in data["accounts"].items()
        }
        return {
            "accounts": accounts,
            "orders": {},
            "trades": [],
            "results": {},
            "daily": {},
            "settings": data,
            "book_index": OrderBook.empty(data["symbols"]),
            "execution_seq": 0,
            "last_seq": 1,
            "last_timestamp": 0,
        }
    fields(command, {"seq", "timestamp", "owner", "kind", "body"})
    if not state:
        raise ValueError("genesis required")
    integer(command["seq"], 2, 1001)
    integer(command["timestamp"], 1, MAX_TIMESTAMP)
    identifier(command["owner"])
    if (
        command["seq"] != state["last_seq"] + 1
        or command["timestamp"] < state["last_timestamp"]
    ):
        raise ValueError("command sequence or timestamp regressed/gapped")
    if command["owner"] not in state["accounts"]:
        raise ValueError("unknown command owner")
    state = copy.deepcopy(state)
    state["last_seq"], state["last_timestamp"] = command["seq"], command["timestamp"]
    owner, body, seq, stamp = (
        command["owner"],
        command["body"],
        command["seq"],
        command["timestamp"],
    )
    kind = command["kind"]
    body = validate(body, kind)
    day = datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
    for old in state["orders"].values():
        if old["status"] in ("OPEN", "PARTIAL") and old["day"] != day:
            release(state, old, "DAY_EXPIRED")
    result = {
        "seq": seq,
        "request_id": body["request_id"],
        "order_id": body["order_id"],
        "accepted": False,
    }

    def reject(reason):
        result["reason"] = reason
        state["results"][str(seq)] = result
        return state

    if kind == "cancel":
        old = state["orders"].get(body["order_id"])
        if not old or old["owner"] != owner:
            return reject("OWNED_ORDER_NOT_FOUND")
        if old["status"] in ("OPEN", "PARTIAL"):
            release(state, old, "USER_CANCEL")
        elif old["status"] != "CANCELED":
            return reject("ORDER_ALREADY_FILLED")
        result.update(accepted=True, status=old["status"])
        state["results"][str(seq)] = result
        return state
    if body["order_id"] in state["orders"]:
        return reject("ORDER_ID_EXISTS")
    symbol, quantity, price = body["symbol"], body["quantity"], body["price"]
    settings = state["settings"]
    if symbol not in settings["symbols"]:
        return reject("UNKNOWN_SYMBOL")
    if not UTCSession().permits(stamp, settings["session"]):
        return reject("SESSION_CLOSED")
    daily_key = owner + ":" + symbol + ":" + day
    if not SubmittedVolumeRisk().permits(
        state["daily"].get(daily_key, 0), quantity, settings["daily_limit"]
    ):
        return reject("DAILY_SUBMITTED_VOLUME_LIMIT")
    account = state["accounts"][owner]
    buy = body["side"] == "BUY"
    funds = account["cash"] if buy else account["positions"][symbol]
    reserve = quantity * price if buy else quantity
    if funds["available"] < reserve:
        return reject("INSUFFICIENT_CASH" if buy else "INSUFFICIENT_SHARES")
    funds["available"] -= reserve
    funds["held"] += reserve
    state["daily"][daily_key] = state["daily"].get(daily_key, 0) + quantity
    order = {
        **body,
        "owner": owner,
        "seq": seq,
        "timestamp": stamp,
        "day": day,
        "remaining": quantity,
        "filled": 0,
        "status": "OPEN",
        "reason": "",
    }
    state["orders"][body["order_id"]] = order
    index = OrderBook(state["book_index"])
    trade_index = 0
    while order["remaining"]:
        maker_id = index.best(symbol, body["side"])
        if maker_id is None:
            break
        maker = state["orders"][maker_id]
        if maker["price"] > price if buy else maker["price"] < price:
            break
        if maker["owner"] == owner:
            release(state, order, "SELF_TRADE_PREVENTION")
            break
        count = min(order["remaining"], maker["remaining"])
        execution_price = maker["price"]
        bid, ask = (order, maker) if buy else (maker, order)
        buyer, seller = state["accounts"][bid["owner"]], state["accounts"][ask["owner"]]
        buyer["cash"]["held"] -= bid["price"] * count
        buyer["cash"]["available"] += (bid["price"] - execution_price) * count
        seller["cash"]["available"] += execution_price * count
        seller["positions"][symbol]["held"] -= count
        buyer["positions"][symbol]["available"] += count
        trade_index += 1
        state["execution_seq"] += 1
        trade_id = "trade_" + str(seq) + "_" + str(trade_index)
        index.consume(maker, count)
        state["trades"].append(
            {
                "id": trade_id,
                "execution_seq": state["execution_seq"],
                "seq": seq,
                "symbol": symbol,
                "price": execution_price,
                "quantity": count,
                "timestamp": stamp,
                "buy_order": bid["order_id"],
                "sell_order": ask["order_id"],
                "buyer": bid["owner"],
                "seller": ask["owner"],
            }
        )
        for row in (order, maker):
            row["remaining"] -= count
            row["filled"] += count
            row["status"] = "FILLED" if not row["remaining"] else "PARTIAL"
    if order["status"] in ("OPEN", "PARTIAL"):
        index.add(order)
    result.update(accepted=True, status=order["status"])
    state["results"][str(seq)] = result
    return state


from .audit import audit as audit
