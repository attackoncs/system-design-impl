"""Independent conservation/reservation and order-book index checks."""


def audit(state):
    settings = state["settings"]
    errors = []
    cash = sum(sum(account["cash"].values()) for account in state["accounts"].values())
    expected_cash = sum(value["cash"] for value in settings["accounts"].values())
    if cash != expected_cash:
        errors.append("cash supply mismatch")
    positions = {}
    for symbol in settings["symbols"]:
        positions[symbol] = sum(
            sum(account["positions"][symbol].values())
            for account in state["accounts"].values()
        )
        if positions[symbol] != sum(
            value["positions"].get(symbol, 0) for value in settings["accounts"].values()
        ):
            errors.append("share supply mismatch: " + symbol)
    for owner, account in state["accounts"].items():
        open_orders = [
            row
            for row in state["orders"].values()
            if row["owner"] == owner and row["status"] in ("OPEN", "PARTIAL")
        ]
        if account["cash"]["held"] != sum(
            row["remaining"] * row["price"]
            for row in open_orders
            if row["side"] == "BUY"
        ):
            errors.append("cash reservation mismatch: " + owner)
        for symbol, balance in account["positions"].items():
            if min(balance.values()) < 0 or balance["held"] != sum(
                row["remaining"]
                for row in open_orders
                if row["side"] == "SELL" and row["symbol"] == symbol
            ):
                errors.append("share reservation mismatch: " + owner)
        if min(account["cash"].values()) < 0:
            errors.append("negative cash: " + owner)
    if state["execution_seq"] != len(state["trades"]) or [
        trade["execution_seq"] for trade in state["trades"]
    ] != list(range(1, len(state["trades"]) + 1)):
        errors.append("outbound execution sequence mismatch")
    active = {
        key: order
        for key, order in state["orders"].items()
        if order["status"] in ("OPEN", "PARTIAL")
    }
    indexed = set()
    try:
        for symbol, sides in state["book_index"].items():
            for side, value in sides.items():
                heap = value["heap"]
                sign = -1 if side == "BUY" else 1
                heap_prices = set(heap)
                if (
                    any(type(price) is not int for price in heap)
                    or any(
                        heap[(index - 1) // 2] > heap[index]
                        for index in range(1, len(heap))
                    )
                    or any(
                        int(price) * sign not in heap_prices
                        for price in value["levels"]
                    )
                ):
                    raise ValueError("book best-price heap mismatch")
                visited = set()
                for price, level in value["levels"].items():
                    cursor, previous, quantity, last_seq = level["head"], None, 0, 0
                    while cursor is not None:
                        if cursor in visited or cursor not in active:
                            raise ValueError("cycle or inactive node")
                        visited.add(cursor)
                        indexed.add(cursor)
                        node, order = value["nodes"][cursor], active[cursor]
                        if (
                            node["previous"] != previous
                            or order["symbol"] != symbol
                            or order["side"] != side
                            or str(order["price"]) != price
                            or node["price"] != price
                            or node["quantity"] != order["remaining"]
                            or order["seq"] <= last_seq
                        ):
                            raise ValueError("book link/priority mismatch")
                        quantity += node["quantity"]
                        previous, cursor, last_seq = cursor, node["next"], order["seq"]
                    if (
                        previous != level["tail"]
                        or quantity != level["quantity"]
                        or quantity <= 0
                    ):
                        raise ValueError("book level quantity mismatch")
                if visited != set(value["nodes"]):
                    raise ValueError("unreachable book node")
        if indexed != set(active):
            raise ValueError("missing active order")
    except (KeyError, TypeError, ValueError):
        errors.append("order-book index mismatch")
    filled = {id: 0 for id in state["orders"]}
    cash_flow = {owner: 0 for owner in state["accounts"]}
    share_flow = {
        owner: {symbol: 0 for symbol in settings["symbols"]}
        for owner in state["accounts"]
    }
    try:
        for trade in state["trades"]:
            buyer, seller = trade["buyer"], trade["seller"]
            bid, ask = (
                state["orders"][trade["buy_order"]],
                state["orders"][trade["sell_order"]],
            )
            if (
                buyer == seller
                or bid["owner"] != buyer
                or ask["owner"] != seller
                or bid["side"] != "BUY"
                or ask["side"] != "SELL"
                or bid["symbol"] != trade["symbol"]
                or ask["symbol"] != trade["symbol"]
                or not ask["price"] <= trade["price"] <= bid["price"]
            ):
                raise ValueError("trade parties/limits mismatch")
            quantity, amount = trade["quantity"], trade["quantity"] * trade["price"]
            filled[trade["buy_order"]] += quantity
            filled[trade["sell_order"]] += quantity
            cash_flow[buyer] -= amount
            cash_flow[seller] += amount
            share_flow[buyer][trade["symbol"]] += quantity
            share_flow[seller][trade["symbol"]] -= quantity
        for owner, account in state["accounts"].items():
            if (
                sum(account["cash"].values())
                != settings["accounts"][owner]["cash"] + cash_flow[owner]
            ):
                raise ValueError("owner cash flow mismatch")
            for symbol, balance in account["positions"].items():
                if (
                    sum(balance.values())
                    != settings["accounts"][owner]["positions"].get(symbol, 0)
                    + share_flow[owner][symbol]
                ):
                    raise ValueError("owner share flow mismatch")
        if any(order["filled"] != filled[id] for id, order in state["orders"].items()):
            raise ValueError("execution quantity mismatch")
    except (KeyError, ValueError, TypeError):
        errors.append("execution/account reconciliation mismatch")
    for order in state["orders"].values():
        if (
            order["filled"] + order["remaining"] != order["quantity"]
            or min(order["filled"], order["remaining"]) < 0
        ):
            errors.append("order fill/remaining mismatch")
    return {
        "matches": not errors,
        "errors": errors,
        "cash_supply": cash,
        "share_supply": positions,
    }
