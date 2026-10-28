"""Canonical business records and bounded query/cursor validation."""

import base64
from datetime import datetime, timezone
import hashlib
import json
import re

from .geo import coordinates


class Error(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identifier(value):
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is None
    ):
        raise ValueError("invalid business ID")
    return value


def validate_business(body):
    if not isinstance(body, dict) or set(body) - {
        "name",
        "latitude",
        "longitude",
        "address",
        "category",
        "hours",
    }:
        raise ValueError("invalid business fields")
    result = {}
    for key, default in (("name", None), ("address", ""), ("category", "other")):
        value = body.get(key, default)
        if (
            not isinstance(value, str)
            or len(value) > 500
            or (key != "address" and not value.strip())
        ):
            raise ValueError(f"invalid {key}")
        result[key] = value
    if any(
        type(body.get(key)) not in (int, float) for key in ("latitude", "longitude")
    ):
        raise ValueError("business coordinates must be numbers")
    result["latitude"], result["longitude"] = coordinates(
        body["latitude"], body["longitude"]
    )
    hours = body.get("hours", [])
    if not isinstance(hours, list) or len(hours) > 100:
        raise ValueError("invalid hours")
    result["hours"] = []
    for window in hours:
        if (
            not isinstance(window, dict)
            or set(window) != {"day", "start", "end"}
            or any(type(window[key]) is not int for key in window)
            or not 0 <= window["day"] <= 6
            or not 0 <= window["start"] < window["end"] <= 1440
        ):
            raise ValueError("hours require UTC day 0..6 and 0 <= start < end <= 1440")
        result["hours"].append(dict(window))
    return result


def query_options(lat, lon, radius, category, open_at, limit):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be 1..1000")
    if category is not None and (
        not isinstance(category, str) or not category.strip() or len(category) > 500
    ):
        raise ValueError("invalid category")
    instant = None
    if open_at is not None:
        if not isinstance(open_at, str) or not 1 <= len(open_at) <= 64:
            raise ValueError("invalid open_at")
        try:
            instant = datetime.fromisoformat(open_at.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                raise ValueError("open_at requires timezone")
            instant = instant.astimezone(timezone.utc)
        except OverflowError:
            raise ValueError("open_at outside UTC datetime range") from None
    fingerprint = hashlib.sha256(
        canonical([lat, lon, radius, category, open_at, limit]).encode()
    ).hexdigest()
    return instant, fingerprint


def cursor_offset(cursor, fingerprint, version, lineage):
    if cursor is None:
        return 0
    if (
        not isinstance(cursor, str)
        or not 1 <= len(cursor) <= 1024
        or re.fullmatch(r"[A-Za-z0-9_-]+", cursor) is None
    ):
        raise ValueError("invalid cursor")
    try:
        raw = base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
        )

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate cursor field")
                result[key] = value
            return result

        token = json.loads(raw, object_pairs_hook=unique)
        if (
            not isinstance(token, dict)
            or set(token) != {"query", "version", "lineage", "offset"}
            or type(token["version"]) is not int
            or token["version"] < 0
            or type(token["offset"]) is not int
            or token["offset"] < 0
            or not isinstance(token["query"], str)
            or not isinstance(token["lineage"], str)
        ):
            raise ValueError()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("invalid cursor") from None
    if (
        token["query"] != fingerprint
        or token["version"] != version
        or token["lineage"] != lineage
    ):
        raise Error(409, "cursor query or snapshot changed")
    return token["offset"]


def next_cursor(fingerprint, version, lineage, offset):
    return (
        base64.urlsafe_b64encode(
            canonical(
                {
                    "query": fingerprint,
                    "version": version,
                    "lineage": lineage,
                    "offset": offset,
                }
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
