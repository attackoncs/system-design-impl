"""Privately owned payment values and bounded provider statements."""

import ipaddress
import json
import math
import re
from urllib.parse import urlsplit

from .common import Error, digest, dumps, identifier, integer, money


def freeze(value):
    return json.loads(dumps(value))


def timestamp(clock):
    value = clock()
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("finite non-negative clock required")
    return value


def receipt_copy(value):
    value = freeze(value)
    fields = {
        "id",
        "merchant",
        "kind",
        "parent",
        "amount",
        "currency",
        "provider_id",
        "status",
        "revision",
        "checkout",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("invalid PSP receipt")
    for key in ("id", "merchant", "provider_id"):
        identifier(value[key])
    money(value["amount"])
    integer(value["revision"], "provider revision", 1)
    if (
        value["kind"] not in ("payin", "payout", "refund")
        or value["currency"] != "USD"
        or value["status"] not in ("PENDING", "SUCCEEDED", "FAILED")
    ):
        raise ValueError("invalid provider state")
    if value["kind"] == "refund":
        identifier(value["parent"])
    elif value["parent"] is not None:
        raise ValueError("only refunds can reference a parent")
    if value["checkout"] is not None:
        target = urlsplit(value["checkout"])
        if (
            value["kind"] != "payin"
            or target.scheme != "http"
            or not target.hostname
            or not ipaddress.ip_address(target.hostname).is_loopback
            or target.username
            or target.password
            or target.query
            or target.fragment
            or not re.fullmatch(r"/checkout/[A-Za-z0-9_-]{1,80}", target.path)
        ):
            raise ValueError("only local simulated checkout URLs accepted")
    return value


def statement_copy(value):
    value = freeze(value)
    if (
        not isinstance(value, dict)
        or set(value) != {"operations", "snapshot", "simulated"}
        or value["simulated"] is not True
        or not isinstance(value["operations"], list)
        or len(value["operations"]) > 10000
    ):
        raise ValueError("invalid complete simulated statement")
    if digest(value["operations"]) != value["snapshot"]:
        raise Error(409, "provider statement checksum mismatch")
    rows = [receipt_copy(row) for row in value["operations"]]
    if len({row["id"] for row in rows}) != len(rows) or len(
        {row["provider_id"] for row in rows}
    ) != len(rows):
        raise Error(409, "duplicate provider statement identity")
    return {**value, "operations": rows}


def job_identity(value):
    if not isinstance(value, dict):
        raise ValueError("job identity required")
    job, token = value.get("id"), value.get("token")
    if not isinstance(job, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{1,160}", job):
        raise ValueError("invalid job id")
    return job, identifier(token)
