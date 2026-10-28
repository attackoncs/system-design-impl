"""Shared validation and canonical records for the nearby-friends domain."""

import json
import math
import re


class Error(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def user_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise Error(400, "invalid user ID")
    return value


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def duration(value):
    # Redis PX uses a signed 64-bit millisecond TTL. Reject values that cannot
    # be represented rather than allowing cache and SQL lifetimes to diverge.
    valid = finite(value) and value > 0
    try:
        valid = valid and 1 <= math.ceil(value * 1000) < 2**63
    except (OverflowError, ValueError):
        valid = False
    if not valid:
        raise ValueError("duration must be finite, positive and fit Redis TTL")
    return float(value)


def location(body):
    if not isinstance(body, dict) or set(body) - {
        "latitude",
        "longitude",
        "timestamp",
        "device",
    }:
        raise Error(400, "invalid location fields")
    lat, lon, stamp = (body.get(key) for key in ("latitude", "longitude", "timestamp"))
    if not all(finite(value) for value in (lat, lon, stamp)):
        raise Error(400, "finite numeric coordinates/timestamp required")
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise Error(400, "coordinates outside allowed range")
    return lat, lon, stamp, user_id(body.get("device", "default"))


def page(offset, limit, maximum=1000):
    if (
        type(offset) is not int
        or not 0 <= offset < 2**63
        or type(limit) is not int
        or not 1 <= limit <= maximum
    ):
        raise Error(400, "invalid history page")


def meters(a, b):
    """Stable spherical distance, including almost-antipodal locations."""
    try:
        lat, lon, other_lat, other_lon = (
            a["latitude"],
            a["longitude"],
            b["latitude"],
            b["longitude"],
        )
    except (KeyError, TypeError):
        raise Error(400, "invalid distance coordinates") from None
    if (
        not all(finite(value) for value in (lat, lon, other_lat, other_lon))
        or not -90 <= lat <= 90
        or not -90 <= other_lat <= 90
        or not -180 <= lon <= 180
        or not -180 <= other_lon <= 180
    ):
        raise Error(400, "invalid distance coordinates")
    p, q = math.radians(lat), math.radians(other_lat)
    delta = math.radians(math.remainder(other_lon - lon, 360))
    sin_p, cos_p, sin_q, cos_q = math.sin(p), math.cos(p), math.sin(q), math.cos(q)
    sin_delta, cos_delta = math.sin(delta), math.cos(delta)
    sine = math.hypot(cos_q * sin_delta, cos_p * sin_q - sin_p * cos_q * cos_delta)
    cosine = sin_p * sin_q + cos_p * cos_q * cos_delta
    return 6371008.8 * math.atan2(sine, cosine)
