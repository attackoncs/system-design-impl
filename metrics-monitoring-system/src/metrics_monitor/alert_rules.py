"""Owned, immutable alert configuration with bounded atomic file imports."""

import json
from pathlib import Path

from .common import dumps, identifier, labels, number

MAX_RULES = 1000


def normalize_rule(body):
    if not isinstance(body, dict) or set(body) - {
        "id",
        "metric",
        "match",
        "aggregation",
        "op",
        "threshold",
        "for_seconds",
        "window",
        "max_age",
        "group_by",
        "destination",
        "channel",
        "labels",
    }:
        raise ValueError("invalid rule fields")
    # This also rejects NaN and detaches the caller's group_by/match containers.
    body = json.loads(dumps(body))
    rule = {
        "match": {},
        "aggregation": "last",
        "for_seconds": 0,
        "window": 300,
        "max_age": 300,
        "group_by": ["instance"],
        "channel": "webhook",
        **body,
    }
    try:
        for key in ("id", "metric", "destination"):
            identifier(rule[key])
        rule["match"] = labels(rule["match"])
        if "labels" in rule:
            rule["labels"] = labels(rule["labels"])
        rule["threshold"] = number(rule["threshold"])
        if rule["op"] not in (">", ">=", "<", "<=", "==") or rule["channel"] not in (
            "webhook",
            "email",
            "phone",
            "pagerduty",
        ):
            raise ValueError("invalid alert operation/channel")
    except KeyError as exc:
        raise ValueError("required rule field missing") from exc
    if rule["aggregation"] not in ("last", "avg", "min", "max", "increase", "rate"):
        raise ValueError("invalid rule aggregation")
    for key, lower, upper in (
        ("for_seconds", 0, 86400),
        ("window", 1, 3600),
        ("max_age", 1, 3600),
    ):
        if not lower <= number(rule[key]) <= upper:
            raise ValueError("invalid rule duration")
    groups = rule["group_by"]
    if (
        not isinstance(groups, list)
        or len(groups) > 8
        or any(not isinstance(key, str) for key in groups)
    ):
        raise ValueError("invalid group_by")
    if len(set(groups)) != len(groups):
        raise ValueError("invalid group_by")
    labels({key: "" for key in groups})
    return rule


def read_rules(path):
    with Path(path).open("rb") as source:
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("rule file exceeds 1 MiB")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate rule field")
            result[key] = value
        return result

    body = json.loads(raw, object_pairs_hook=pairs)
    if isinstance(body, dict):
        if set(body) != {"rules"}:
            raise ValueError("rule file requires an array or rules object")
        body = body["rules"]
    if not isinstance(body, list) or not 1 <= len(body) <= MAX_RULES:
        raise ValueError("rule import requires 1..1000 rules")
    return body
