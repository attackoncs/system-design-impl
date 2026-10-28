"""Persist a bounded checkout's independent merchant pay-ins atomically."""

import json

from .common import Error, digest, dumps, identifier, money
from .contracts import freeze


class CheckoutMixin:
    def create_checkout(self, body):
        body = freeze(body)
        if not isinstance(body, dict) or set(body) != {"checkout_id", "orders"}:
            raise ValueError("checkout requires checkout_id and orders")
        checkout = identifier(body["checkout_id"])
        orders = body["orders"]
        if not isinstance(orders, list) or not 1 <= len(orders) <= 20:
            raise ValueError("checkout requires 1..20 independent payment orders")
        seen = set()
        for order in orders:
            if not isinstance(order, dict) or set(order) != {
                "merchant",
                "order_id",
                "amount",
                "currency",
            }:
                raise ValueError("invalid payment order")
            identity = identifier(order["merchant"]), identifier(order["order_id"])
            money(order["amount"])
            if order["currency"] != "USD" or identity in seen:
                raise ValueError("duplicate order or unsupported currency")
            seen.add(identity)
        fingerprint = digest(body)
        with self.transaction(True) as db:
            old = db.execute(
                "SELECT * FROM checkouts WHERE id=?", (checkout,)
            ).fetchone()
            if old:
                if old["hash"] != fingerprint:
                    raise Error(409, "checkout idempotency content conflict")
                identities = json.loads(old["payload"])
            else:
                identities = []
                for index, order in enumerate(orders):
                    request = {
                        "request_key": "checkout_" + digest([checkout, index]),
                        "order_id": order["order_id"],
                        "amount": order["amount"],
                        "currency": order["currency"],
                    }
                    operation = self.create(order["merchant"], "payin", request, db=db)
                    identities.append(operation["id"])
                db.execute(
                    "INSERT INTO checkouts VALUES(?,?,?,?)",
                    (checkout, fingerprint, dumps(identities), self.clock()),
                )
            return {
                "checkout_id": checkout,
                "orders": [self.operation(op, db=db) for op in identities],
            }

    def checkout(self, checkout):
        identifier(checkout)
        with self.transaction() as db:
            row = db.execute(
                "SELECT payload FROM checkouts WHERE id=?", (checkout,)
            ).fetchone()
            if not row:
                raise Error(404, "checkout missing")
            return {
                "checkout_id": checkout,
                "orders": [
                    self.operation(op, db=db) for op in json.loads(row["payload"])
                ],
            }
