"""Strict, bounded WebSocket JSON messages, separate from domain operations."""

import json
import math

from .core import Error

MAX_MESSAGE = 16384
MAX_OUTPUT = 256 * 1024

_FIELDS = {
    "location": {"latitude", "longitude", "timestamp", "device"},
    "friend.request": {"target"},
    "friend.accept": {"target"},
    "friend.remove": {"target"},
    "sharing": {"enabled"},
    "history.consent": {"enabled"},
    # target is retained for compatibility, but history is always actor-only.
    "history": {"offset", "limit", "target"},
    "ring": {"nodes", "expected_epoch"},
    "snapshot": {"radius_m", "offset", "limit"},
}


def validate(message):
    if not isinstance(message, dict):
        raise Error(400, "JSON object required")
    op = message.get("op")
    if not isinstance(op, str) or op not in _FIELDS:
        raise Error(400, "unknown operation")
    if set(message) - (_FIELDS[op] | {"op", "request_id"}):
        raise Error(400, "unknown operation field")
    request_id = message.get("request_id")
    if request_id is not None and (
        not isinstance(request_id, str) or not 1 <= len(request_id) <= 64
    ):
        raise Error(400, "invalid request_id")
    return message


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("non-finite JSON number")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def parse(raw):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_MESSAGE:
        raise Error(400, "bounded UTF-8 text message required")
    return json.loads(
        raw,
        object_pairs_hook=_pairs,
        parse_constant=_constant,
        parse_float=_float,
    )
