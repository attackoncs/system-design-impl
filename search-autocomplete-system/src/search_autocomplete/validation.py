"""Shared argument predicates for collection, storage, scheduling and serving."""

import math


def finite_number(value):
    """Accept real int/float values only; bool and huge-int overflow are rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def integer_in(value, low, high=None):
    """Accept a non-bool integer within inclusive bounds."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= low
        and (high is None or value <= high)
    )


def require_integer(value, low, high=None, *, message):
    if not integer_in(value, low, high):
        raise ValueError(message)
    return value


def require_shard_count(value):
    """A build splits its snapshot into 1..256 lexical shards."""
    return require_integer(value, 1, 256, message="invalid shard count")


def require_event_id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 200:
        raise ValueError("invalid event ID")
    return value


def event_time(value):
    """Return whether an explicit event timestamp is a finite, non-negative epoch."""
    return finite_number(value) and value >= 0
