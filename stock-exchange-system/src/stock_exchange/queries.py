"""Public depth/OHLCV and private bilateral execution read models."""

from .common import Error, integer


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
                        "execution_seq": trade["execution_seq"],
                        "timestamp": trade["timestamp"],
                    }
                )
    return {
        "seq": value["seq"],
        "orders": [row for row in value["orders"].values() if row["owner"] == owner],
        "fills": fills,
    }
