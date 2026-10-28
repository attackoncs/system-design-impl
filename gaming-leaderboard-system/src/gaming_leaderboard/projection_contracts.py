"""Private immutable query/feed values at the Redis projection boundary."""

import json
import re

from .common import (
    Error,
    digest,
    dumps,
    identifier,
    integer,
    lex_member,
    month_bounds,
    shard_for,
)


def batch_copy(value, shard, shards):
    if not isinstance(value, dict) or set(value) != {
        "lineage",
        "shard",
        "head",
        "after",
        "events",
    }:
        raise ValueError("invalid feed fields")
    value = json.loads(dumps(value))
    identifier(value["lineage"])
    integer(value["shard"], "shard", 0, shards - 1)
    if value["shard"] != shard:
        raise Error(409, "wrong event shard")
    after = integer(value["after"], "after", 0, 2**53 - 1)
    head = integer(value["head"], "head", after, 2**53 - 1)
    if not isinstance(value["events"], list) or len(value["events"]) > 200:
        raise ValueError("feed permits at most 200 events")
    expected = after
    for entry in value["events"]:
        if not isinstance(entry, dict) or set(entry) != {"seq", "payload", "hash"}:
            raise ValueError("invalid feed event")
        integer(entry["seq"], "seq", expected + 1, expected + 1)
        p = entry["payload"]
        if not isinstance(p, dict) or set(p) != {"month", "user", "score"}:
            raise ValueError("invalid score payload")
        month_bounds(p["month"])
        identifier(p["user"])
        integer(p["score"], "score", 0, 1_000_000_000)
        if shard_for(p["user"], shards) != shard or entry["hash"] != digest(p):
            raise Error(409, "event checksum or ownership mismatch")
        expected = entry["seq"]
    if expected > head or (not value["events"] and head != after):
        raise Error(409, "feed head disagrees with event page")
    return value


def query_copy(value, shard, shards):
    if not isinstance(value, dict):
        raise ValueError("query object required")
    value = json.loads(dumps(value))
    modes = {
        "top": {"limit"},
        "target": {"user"},
        "around": {"pivot", "score", "radius"},
        "counts": {"scores"},
        "dump": set(),
    }
    mode = value.get("mode")
    if mode not in modes:
        raise ValueError("invalid query mode")
    required = {"month", "lineage", "minimum", "mode"} | modes[mode]
    if not required <= set(value) or set(value) - required - {"epoch"}:
        raise ValueError("invalid query fields")
    month_bounds(value["month"])
    identifier(value["lineage"])
    integer(value["minimum"], "minimum", 0, 2**53 - 1)
    if "epoch" in value:
        identifier(value["epoch"])
    if mode == "top":
        integer(value["limit"], "limit", 1, 10)
    elif mode == "target":
        identifier(value["user"])
        if shard_for(value["user"], shards) != shard:
            raise Error(409, "target belongs to another projection shard")
    elif mode == "around":
        integer(value["radius"], "radius", 0, 4)
        integer(value["score"], "score", 0, 1_000_000_000)
        pivot = value["pivot"]
        if not isinstance(pivot, str) or not re.fullmatch(
            r"[0-9]{10}:[A-Za-z0-9_-]{1,64}", pivot
        ):
            raise ValueError("invalid query pivot")
        if pivot != lex_member(pivot[11:], value["score"]):
            raise ValueError("pivot disagrees with score")
    elif mode == "counts":
        scores = value["scores"]
        if not isinstance(scores, list) or not 1 <= len(scores) <= 9:
            raise ValueError("counts permits 1..9 scores")
        for score in scores:
            integer(score, "score", 0, 1_000_000_000)
        if len(set(scores)) != len(scores):
            raise ValueError("repeated query score")
    value["shard"] = shard
    return value
