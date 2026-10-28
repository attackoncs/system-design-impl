"""Owned, strict contracts for simulated payment intents and receipts."""

import json
import re

from .common import Error, booking_hotel, dumps, integer

OPERATION_FIELDS = {"id", "action", "booking_id", "amount", "currency", "method"}


def normalize_operation(body):
    if not isinstance(body, dict) or set(body) != OPERATION_FIELDS:
        raise ValueError("invalid simulated operation fields")
    owned = json.loads(dumps(body))
    booking = owned["booking_id"]
    if not isinstance(booking, str):
        raise ValueError("invalid reservation ID")
    booking_hotel(booking)
    if (
        owned["action"] not in ("charge", "refund")
        or owned["id"] != owned["action"] + ":" + booking
    ):
        raise ValueError("operation ID must match action and reservation")
    integer(owned["amount"], "amount", 0, 30_000_000_000)
    if owned["currency"] != "USD" or owned["method"] not in ("test-ok", "test-decline"):
        raise ValueError("only USD simulated payment methods are supported")
    return owned


def normalize_job(body):
    if not isinstance(body, dict) or set(body) != OPERATION_FIELDS | {"token"}:
        raise ValueError("invalid payment job fields")
    owned = json.loads(dumps(body))
    operation = normalize_operation({key: owned[key] for key in OPERATION_FIELDS})
    token = owned["token"]
    if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{32}", token) is None:
        raise ValueError("invalid job lease token")
    return {**operation, "token": token}


def normalize_receipt(body, job):
    required = {"booking_id", "amount", "currency", "status"}
    if (
        not isinstance(body, dict)
        or not required <= set(body)
        or set(body) - required - {"simulated"}
    ):
        raise Error(409, "invalid simulated payment receipt")
    owned = json.loads(dumps(body))
    try:
        integer(owned["amount"], "amount", 0, 30_000_000_000)
    except ValueError:
        raise Error(409, "invalid simulated payment amount") from None
    if (owned["booking_id"], owned["amount"], owned["currency"]) != (
        job["booking_id"],
        job["amount"],
        "USD",
    ):
        raise Error(409, "provider receipt does not match frozen order")
    statuses = ("paid", "declined") if job["action"] == "charge" else ("refunded",)
    if owned["status"] not in statuses or (
        "simulated" in owned and owned["simulated"] is not True
    ):
        raise Error(409, "unexpected simulated payment outcome")
    return owned
