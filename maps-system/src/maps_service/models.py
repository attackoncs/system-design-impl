"""Canonical records and validation shared by catalog and telemetry paths."""

import json
import math
import re


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


MODES = {"driving", "walking", "cycling"}


class Error(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("invalid identifier")
    return value


def positive(value, maximum=1e8):
    if type(value) not in (int, float) or not finite(value) or not 0 < value <= maximum:
        raise ValueError("invalid positive numeric value")
    return float(value)


def report(value):
    if not isinstance(value, dict) or set(value) - {
        "event_id",
        "sequence",
        "timestamp",
        "latitude",
        "longitude",
        "mode",
        "heading",
    }:
        raise ValueError("invalid report fields")
    result = dict(value)
    identifier(result.get("event_id"))
    sequence = result.get("sequence")
    if type(sequence) is not int or not 0 <= sequence < 2**53:
        raise ValueError("invalid report sequence")
    if not finite(result.get("timestamp")):
        raise ValueError("invalid report timestamp")
    mode = result.get("mode", "driving")
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError("invalid report mode")
    heading = result.get("heading")
    if heading is not None and (not finite(heading) or not 0 <= heading < 360):
        raise ValueError("invalid heading")
    return result
