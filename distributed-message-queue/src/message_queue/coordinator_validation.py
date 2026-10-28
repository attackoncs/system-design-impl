"""Shared strict numeric and tag validation for coordination operations."""

import math

from .common import name


def finite_number(value, minimum, maximum, label):
    if type(value) not in (int, float) or not minimum <= value <= maximum:
        raise ValueError(label)
    if not math.isfinite(value):
        raise ValueError(label)
    return value


def tags_filter(tags):
    if tags is None:
        return None
    if not isinstance(tags, list) or not 1 <= len(tags) <= 16:
        raise ValueError("filter requires 1..16 tags")
    result = [name(tag) for tag in tags]
    if len(set(result)) != len(result):
        raise ValueError("tags must be unique")
    return frozenset(result)
