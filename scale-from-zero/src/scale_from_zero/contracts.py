"""Frozen profile, replication and outbox values shared across adapters."""

import math
import re

from .common import checksum, encode, identifier, integer


def freeze(value):
    from .transport import loads

    return loads(encode(value))


def fields(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError("exact fields required: " + ", ".join(sorted(expected)))
    return value


def timestamp(value):
    try:
        valid = (
            type(value) in (int, float)
            and math.isfinite(value)
            and 0 <= value <= 10**12
        )
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("finite nonnegative clock required")
    return value


def profile(value):
    fields(value, {"display_name", "city"})
    if any(not isinstance(text, str) or len(text) > 128 for text in value.values()):
        raise ValueError("profile strings are limited to 128 characters")
    return value


def update(value):
    value = freeze(value)
    fields(value, {"request_id", "expected_version", "display_name", "city"})
    identifier(value["request_id"])
    integer(value["expected_version"])
    profile({key: value[key] for key in ("display_name", "city")})
    return value


def record(value, user=None):
    value = freeze(value)
    fields(value, {"user", "version", "profile"})
    identifier(value["user"])
    integer(value["version"], 1, 1000)
    profile(value["profile"])
    if user is not None and value["user"] != user:
        raise ValueError("profile response belongs to another user")
    return value


def change(value):
    value = freeze(value)
    fields(value, {"seq", "user", "version", "profile", "hash"})
    integer(value["seq"], 1, 1000)
    record({key: value[key] for key in ("user", "version", "profile")})
    if (
        checksum({key: value[key] for key in ("seq", "user", "version", "profile")})
        != value["hash"]
    ):
        raise ValueError("replication checksum mismatch")
    return value


def session(user, token):
    identifier(user)
    if not isinstance(token, str) or not re.fullmatch(
        re.escape(user) + r"\.[0-9a-f]{48}", token
    ):
        raise ValueError("invalid session token")
    return token


def job(value):
    value = freeze(value)
    fields(value, {"id", "token", "payload", "lease", "attempts", "source_id"})
    if not isinstance(value["source_id"], str) or not re.fullmatch(
        r"[0-9a-f]{32}", value["source_id"]
    ):
        raise ValueError("job source database identity required")
    payload = value["payload"]
    fields(payload, {"seq", "user", "version", "profile"})
    integer(payload["seq"], 1, 1000)
    record({key: payload[key] for key in ("user", "version", "profile")})
    if value["id"] != payload["user"] + "_" + str(payload["version"]):
        raise ValueError("job identity differs from payload")
    if not isinstance(value["token"], str) or not re.fullmatch(
        r"[0-9a-f]{32}", value["token"]
    ):
        raise ValueError("claim token required")
    timestamp(value["lease"])
    integer(value["attempts"], 1)
    return value
