"""Serializable price levels, FIFO links and order-id index.

Link operations are O(1); creating/best-removing a price level uses O(log P).
The surrounding reference still copies/serializes the complete state per command.
"""

import heapq


class OrderBook:
    def __init__(self, value):
        self.value = value

    @classmethod
    def empty(cls, symbols):
        return {
            symbol: {
                side: {"levels": {}, "heap": [], "nodes": {}}
                for side in ("BUY", "SELL")
            }
            for symbol in symbols
        }

    def side(self, order):
        return self.value[order["symbol"]][order["side"]]

    def add(self, order):
        side = self.side(order)
        key, id = str(order["price"]), order["order_id"]
        if id in side["nodes"]:
            raise ValueError("order already indexed")
        if key not in side["levels"]:
            side["levels"][key] = {"head": None, "tail": None, "quantity": 0}
            heapq.heappush(
                side["heap"], order["price"] * (-1 if order["side"] == "BUY" else 1)
            )
        level = side["levels"][key]
        previous = level["tail"]
        side["nodes"][id] = {
            "previous": previous,
            "next": None,
            "price": key,
            "quantity": order["remaining"],
        }
        if previous is None:
            level["head"] = id
        else:
            side["nodes"][previous]["next"] = id
        level["tail"] = id
        level["quantity"] += order["remaining"]

    def remove(self, order):
        side, id = self.side(order), order["order_id"]
        node = side["nodes"].pop(id, None)
        if node is None:
            return
        level = side["levels"][node["price"]]
        previous, following = node["previous"], node["next"]
        if previous is None:
            level["head"] = following
        else:
            side["nodes"][previous]["next"] = following
        if following is None:
            level["tail"] = previous
        else:
            side["nodes"][following]["previous"] = previous
        level["quantity"] -= node["quantity"]
        if level["head"] is None:
            del side["levels"][node["price"]]
        if len(side["heap"]) > 2 * len(side["levels"]) + 8:
            sign = -1 if order["side"] == "BUY" else 1
            side["heap"][:] = [
                int(price) * sign for price in sorted(side["levels"], key=int)
            ]
            heapq.heapify(side["heap"])

    def consume(self, order, quantity):
        side = self.side(order)
        node = side["nodes"][order["order_id"]]
        if quantity == node["quantity"]:
            self.remove(order)
        elif 0 < quantity < node["quantity"]:
            node["quantity"] -= quantity
            side["levels"][node["price"]]["quantity"] -= quantity
        else:
            raise ValueError("invalid indexed execution quantity")

    def best(self, symbol, taker_side):
        maker_side = "SELL" if taker_side == "BUY" else "BUY"
        side = self.value[symbol][maker_side]
        sign = -1 if maker_side == "BUY" else 1
        while side["heap"]:
            key = str(side["heap"][0] * sign)
            level = side["levels"].get(key)
            if level is not None:
                return level["head"]
            heapq.heappop(side["heap"])
        return None

    def levels(self, symbol):
        return {
            name: {
                price: level["quantity"]
                for price, level in self.value[symbol][side]["levels"].items()
            }
            for name, side in (("bids", "BUY"), ("asks", "SELL"))
        }
