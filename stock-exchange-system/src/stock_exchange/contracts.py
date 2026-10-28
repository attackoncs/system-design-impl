"""Frozen wire values and deterministic matching contracts."""

from .common import encode, identifier, integer

MAX_TIMESTAMP = 253402300799


def freeze(value):
    from .transport import loads

    return loads(encode(value))


def fields(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError("exact fields required: " + ", ".join(sorted(expected)))
    return value


def validate(body, kind):
    body = freeze(body)
    required = {"request_id", "order_id"} | (
        {"symbol", "side", "price", "quantity"} if kind == "new" else set()
    )
    if kind not in {"new", "cancel"}:
        raise ValueError("limit order or cancel required")
    fields(body, required)
    identifier(body["request_id"])
    identifier(body["order_id"])
    if kind == "new":
        identifier(body["symbol"])
        integer(body["price"], 1, 100_000_000)
        integer(body["quantity"], 1, 1_000_000)
        if body["side"] not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
    return body


def settings(value):
    value = freeze(value)
    fields(value, {"symbols", "accounts", "session", "daily_limit"})
    symbols = value["symbols"]
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 100:
        raise ValueError("1..100 symbols required")
    for symbol in symbols:
        identifier(symbol)
    if len(set(symbols)) != len(symbols):
        raise ValueError("duplicate symbol")
    if (
        not isinstance(value["accounts"], dict)
        or not 1 <= len(value["accounts"]) <= 100
    ):
        raise ValueError("1..100 accounts required")
    for owner, account in value["accounts"].items():
        identifier(owner)
        fields(account, {"cash", "positions"})
        integer(account["cash"])
        if not isinstance(account["positions"], dict) or set(
            account["positions"]
        ) - set(symbols):
            raise ValueError("unknown opening position")
        for quantity in account["positions"].values():
            integer(quantity)
    session = fields(value["session"], {"weekdays", "start_minute", "end_minute"})
    integer(session["start_minute"], 0, 1439)
    integer(session["end_minute"], session["start_minute"] + 1, 1440)
    if not isinstance(session["weekdays"], list) or not session["weekdays"]:
        raise ValueError("UTC weekday list required")
    for day in session["weekdays"]:
        integer(day, 0, 6)
    if len(set(session["weekdays"])) != len(session["weekdays"]):
        raise ValueError("duplicate weekday")
    integer(value["daily_limit"], 1, 1_000_000)
    return value


def hash_value(value):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError("SHA256 hex value required")
    return value
