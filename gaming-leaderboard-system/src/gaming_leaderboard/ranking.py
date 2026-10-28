"""Validate shard candidates before computing exact global ranks/windows."""

from .common import Error, identifier, integer, lex_member, shard_for


def row(value, shard, shards):
    try:
        if not isinstance(value, dict) or set(value) != {"user", "score", "lex"}:
            raise ValueError("invalid candidate fields")
        user = identifier(value["user"])
        score = integer(value["score"], "candidate score", 0, 1_000_000_000)
        if shard_for(user, shards) != shard or value["lex"] != lex_member(user, score):
            raise ValueError("candidate identity/index mismatch")
        return dict(value)
    except (ValueError, KeyError, TypeError):
        raise Error(502, "invalid projection candidate") from None


def rows(values, shard, shards, maximum, *, descending=False):
    if not isinstance(values, list) or len(values) > maximum:
        raise Error(502, "projection candidate budget exceeded")
    result = [row(value, shard, shards) for value in values]
    keys = [value["lex"] for value in result]
    if len(set(value["user"] for value in result)) != len(result) or keys != sorted(
        keys, reverse=descending
    ):
        raise Error(502, "projection candidate order or uniqueness invalid")
    return result


def top(parts, shards):
    candidates = [
        candidate
        for shard, part in enumerate(parts)
        for candidate in rows(part.get("rows"), shard, shards, 10)
    ]
    result = sorted(candidates, key=lambda item: item["lex"])[:10]
    rank, previous = 0, None
    for position, value in enumerate(result, 1):
        if value["score"] != previous:
            rank = position
        value.update(rank=rank, position=position)
        previous = value["score"]
    return result


def neighbors(target, parts, radius, shards):
    above, below, preceding = [], [], 0
    for shard, part in enumerate(parts):
        before = part.get("before")
        if type(before) is not int or not 0 <= before <= 10000:
            raise Error(502, "invalid projection position count")
        preceding += before
        higher = part.get("higher")
        if type(higher) is not int or not 0 <= higher <= before:
            raise Error(502, "invalid higher-score count")
        before_rows = rows(part.get("above"), shard, shards, radius, descending=True)
        after_rows = rows(part.get("below"), shard, shards, radius)
        if any(value["lex"] >= target["lex"] for value in before_rows) or any(
            value["lex"] <= target["lex"] for value in after_rows
        ):
            raise Error(502, "projection neighbors crossed pivot")
        above.extend(before_rows)
        below.extend(after_rows)
    if preceding >= 10000:
        raise Error(502, "global position exceeds reference budget")
    above = sorted(above, key=lambda item: item["lex"])[-radius:] if radius else []
    below = sorted(below, key=lambda item: item["lex"])[:radius]
    return above + [target] + below, 1 + preceding - len(above)


def ranks(result, counts, start, shards):
    wanted = {str(value["score"]) for value in result}
    for part in counts:
        values = part.get("counts")
        if (
            not isinstance(values, dict)
            or set(values) != wanted
            or any(type(v) is not int or not 0 <= v <= 10000 for v in values.values())
        ):
            raise Error(502, "invalid strict-higher counts")
    for position, value in enumerate(result, start):
        rank = 1 + sum(part["counts"][str(value["score"])] for part in counts)
        if not 1 <= rank <= position <= 10000:
            raise Error(502, "inconsistent global rank/position")
        value.update(position=position, rank=rank)
